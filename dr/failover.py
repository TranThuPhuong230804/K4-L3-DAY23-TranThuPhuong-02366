"""BƯỚC 3b — SINH VIÊN VIẾT. Cutover sang region phụ.

5 bước, THỨ TỰ QUAN TRỌNG (§2 Kiến Trúc Tham Chiếu: DNS/LB, compute, state là 3 lớp riêng):
  1_verify_target    — /v1/state của region phụ: weights? vector count? pool_state?
  2_restore_snapshot — gọi state/snapshot.py get + state/snapshot.py rpo()
                       Log BẮT BUỘC: rpo_seconds, docs_lost, embed_model_version.
                       (§3: "backup index nhưng quên backup embedding model version
                        -> index không tương thích khi restore")
  3_scale_pool       — ghi "full" vào state/region-<t>/pool_state (warm -> full)
  4_wait_ready       — POLL /readyz tới khi 200. Region phụ có WARMUP_SECONDS —
                       đây là GPU pool warm-up của §4, nó nằm trong RTO của bạn.
  5_dns_cutover      — ghi region đích vào edge/active_region

BẪY: nếu bạn đổi edge/active_region TRƯỚC bước 4, user sẽ nhận 503 từ CẢ HAI region
và RTO của bạn dài hơn, không ngắn hơn. Nếu bước 4 timeout -> ABORT, KHÔNG cutover.

Mỗi bước ghi 1 dòng vào reports/failover-events.jsonl với ts + step.
Không có dòng 5_dns_cutover = tools/measure_rto.py không tìm được t_cutover = mất điểm.

Chạy:  python dr/failover.py --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from state import snapshot  # noqa: E402

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
LOG = pathlib.Path("reports/failover-events.jsonl")
ACTIVE_REGION = pathlib.Path("edge/active_region")


def emit(**kw):
    """Append một sự kiện failover có timestamp vào log JSONL."""
    now = time.time()
    event = {
        "ts": now,
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now)),
        **kw,
    }
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as log:
        log.write(json.dumps(event, ensure_ascii=False) + "\n")
    print("FAILOVER", json.dumps(event, ensure_ascii=False))
    return event


def state_of(region: str) -> dict:
    """Đọc trạng thái compute và dữ liệu hiện tại của một region."""
    response = httpx.get(f"{URL[region]}/v1/state", timeout=2.0)
    if response.status_code != 200:
        raise RuntimeError(f"region-{region} state returned HTTP {response.status_code}")
    return response.json()


def failover(target: str, backend: str, wait: float) -> dict:
    """Khôi phục target, chờ ready rồi mới chuyển edge sang target."""
    if target not in URL:
        raise ValueError(f"unknown target region: {target}")
    if backend not in {"fs", "minio"}:
        raise ValueError(f"unsupported backend: {backend}")
    if wait < 0:
        raise ValueError("wait must not be negative")

    completed = []
    result = {
        "ok": False,
        "target": target,
        "backend": backend,
        "rpo_seconds": None,
        "docs_lost": None,
        "embed_model_version": None,
        "steps": completed,
        "steps_completed": completed,
    }

    # 1. Target phải tồn tại và trả lời được, dù nó chưa sẵn sàng phục vụ traffic.
    try:
        initial_state = state_of(target)
    except (httpx.RequestError, ValueError, RuntimeError) as exc:
        error = f"verify_target_failed: {type(exc).__name__}: {exc}"
        emit(step="1_verify_target", target=target, ok=False, error=error)
        result["error"] = error
        return result

    emit(step="1_verify_target", target=target, ok=True, state=initial_state)
    completed.append("1_verify_target")

    # 2. Restore cả vector DB lẫn model weights rồi đo RPO từ dữ liệu thật.
    try:
        restored = snapshot.get(target, backend)
        source = restored.get("source_region") or ("a" if target == "b" else "b")
        rpo = snapshot.rpo(
            pathlib.Path(f"state/region-{source}/vectors.sqlite"),
            pathlib.Path(f"state/region-{target}/vectors.sqlite"),
        )
    except (Exception, SystemExit) as exc:
        error = f"restore_snapshot_failed: {type(exc).__name__}: {exc}"
        emit(step="2_restore_snapshot", target=target, backend=backend,
             ok=False, error=error)
        result["error"] = error
        return result

    result.update(
        rpo_seconds=rpo["rpo_seconds"],
        docs_lost=rpo["docs_lost"],
        embed_model_version=restored.get("embed_model_version"),
    )
    emit(
        step="2_restore_snapshot",
        target=target,
        backend=backend,
        ok=True,
        snapshot_at=restored.get("snapshot_at"),
        restored_at=restored.get("restored_at"),
        rpo_seconds=result["rpo_seconds"],
        docs_lost=result["docs_lost"],
        embed_model_version=result["embed_model_version"],
    )
    completed.append("2_restore_snapshot")

    # 3. Bật pool. serving/app.py sẽ bắt đầu bộ đếm warm-up khi thấy transition này.
    pool_state = pathlib.Path(f"state/region-{target}/pool_state")
    try:
        pool_state.parent.mkdir(parents=True, exist_ok=True)
        pool_state.write_text("full\n", encoding="utf-8")
    except OSError as exc:
        error = f"scale_pool_failed: {type(exc).__name__}: {exc}"
        emit(step="3_scale_pool", target=target, ok=False, error=error)
        result["error"] = error
        return result

    emit(step="3_scale_pool", target=target, ok=True, pool_state="full")
    completed.append("3_scale_pool")

    # 4. Không tin file pool_state; chỉ readiness endpoint mới quyết định được cutover.
    started = time.monotonic()
    deadline = started + wait
    attempts = 0
    last_reason = "readiness timeout"
    ready_body = None

    while time.monotonic() < deadline:
        attempts += 1
        remaining = max(0.001, deadline - time.monotonic())
        try:
            response = httpx.get(
                f"{URL[target]}/readyz",
                timeout=min(2.0, remaining),
            )
            if response.status_code == 200:
                ready_body = response.json()
                break
            try:
                body = response.json()
                reasons = body.get("reasons") if isinstance(body, dict) else None
            except ValueError:
                reasons = None
            last_reason = f"status={response.status_code}"
            if reasons:
                last_reason += f": {', '.join(map(str, reasons))}"
        except httpx.RequestError as exc:
            last_reason = f"{type(exc).__name__}: {exc}"

        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(0.5, remaining))

    waited_s = round(time.monotonic() - started, 3)
    if ready_body is None:
        emit(
            step="4_wait_ready",
            target=target,
            ok=False,
            attempts=attempts,
            waited_s=waited_s,
            error=last_reason,
        )
        result.update(error=f"target_not_ready: {last_reason}", waited_s=waited_s)
        return result

    emit(
        step="4_wait_ready",
        target=target,
        ok=True,
        attempts=attempts,
        waited_s=waited_s,
        readiness=ready_body,
    )
    completed.append("4_wait_ready")

    # Lấy state sau restore/warm-up để runbook có bằng chứng về weights và vector count.
    try:
        result["target_state"] = state_of(target)
    except (httpx.RequestError, ValueError, RuntimeError):
        result["target_state"] = ready_body

    # 5. Chỉ được chạm active_region sau khi bước 4 đã thành công.
    try:
        previous = ACTIVE_REGION.read_text(encoding="utf-8").strip() \
            if ACTIVE_REGION.exists() else None
        ACTIVE_REGION.parent.mkdir(parents=True, exist_ok=True)
        ACTIVE_REGION.write_text(f"{target}\n", encoding="utf-8")
    except OSError as exc:
        error = f"dns_cutover_failed: {type(exc).__name__}: {exc}"
        emit(step="5_dns_cutover", target=target, ok=False, error=error)
        result["error"] = error
        return result

    emit(step="5_dns_cutover", target=target, previous=previous, active=target, ok=True)
    completed.append("5_dns_cutover")
    result["ok"] = True
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--wait", type=float, default=60)
    a = p.parse_args()
    print(json.dumps(failover(a.target, a.backend, a.wait), indent=2))
