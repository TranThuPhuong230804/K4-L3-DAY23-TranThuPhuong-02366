"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import json
import math
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402
from dr import health_checker as hc  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def step(n, name, **kw):
    """Ghi một bước runbook có timestamp vào log JSONL."""
    now = time.time()
    event = {
        "ts": now,
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now)),
        "step": n,
        "name": name,
        **kw,
    }
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as log:
        log.write(json.dumps(event, ensure_ascii=False) + "\n")
    print("RUNBOOK", json.dumps(event, ensure_ascii=False))
    return event


def confirm(auto: bool, msg: str) -> bool:
    """Tự xác nhận trong CI, còn chạy tay thì yêu cầu operator nhập y/yes."""
    if auto:
        return True
    return input(f"{msg} [y/N] ").strip().lower() in {"y", "yes"}


def latest_outage(primary: str) -> dict | None:
    """Lấy sự kiện kill mới nhất của primary để tính notification delay."""
    path = pathlib.Path("chaos/chaos-events.jsonl")
    if not path.exists():
        return None
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("action") == "kill" and event.get("region") == primary:
            events.append(event)
    return events[-1] if events else None


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    """Thực thi bảy bước của runbook region-down theo thứ tự."""
    if primary not in URL or target not in URL:
        raise ValueError("primary and target must be one of: a, b")
    if primary == target:
        raise ValueError("primary and target must be different regions")

    started = time.time()
    result = {"ok": False, "primary": primary, "target": target, "backend": backend}

    # 1. Xác nhận bằng ba probe liên tiếp; một lỗi đơn lẻ không đủ tuyên bố outage.
    probes = []
    for attempt in range(1, 4):
        primary_ready, primary_reason = hc.probe(primary, timeout=2.0)
        target_ready, target_reason = hc.probe(target, timeout=2.0)
        probes.append({
            "attempt": attempt,
            "primary_ready": primary_ready,
            "primary_reason": primary_reason,
            "target_ready": target_ready,
            "target_reason": target_reason,
        })
        if attempt < 3:
            time.sleep(2.0)

    outage_confirmed = all(not p["primary_ready"] for p in probes)
    step(1, "xac_nhan_outage", primary=primary, target=target,
         confirmed=outage_confirmed, probes=probes)
    if not outage_confirmed:
        result["error"] = "primary outage was not confirmed by 3 consecutive probes"
        return result

    # 2. Ghi rõ thời điểm outage và thời điểm operator nhận incident.
    outage = latest_outage(primary)
    incident_at = time.time()
    outage_ts = outage.get("ts") if outage else None
    notification_delay = None if outage_ts is None else round(incident_at - outage_ts, 3)
    approved = confirm(auto, f"Region {primary} down. Fail over to region {target}?")
    step(
        2,
        "thong_bao_incident",
        primary=primary,
        target=target,
        outage_ts=outage_ts,
        outage_iso=outage.get("iso") if outage else None,
        operator_notified_ts=incident_at,
        notification_delay_s=notification_delay,
        approved=approved,
    )
    if not approved:
        result["error"] = "operator declined failover"
        return result

    # 3. Hàm failover chịu trách nhiệm restore, scale, wait-ready và cutover đúng một lần.
    failover_result = fo.failover(target, backend, wait=60.0)
    step(3, "scale_gpu_pool", target=target, failover_ok=failover_result.get("ok"),
         failover_steps=failover_result.get("steps_completed", []),
         error=failover_result.get("error"))
    result["failover"] = failover_result
    if not failover_result.get("ok"):
        result["error"] = failover_result.get("error", "failover failed")
        return result

    # 4. Chỉ đọc kết quả đã trả về; tuyệt đối không chạy failover lần thứ hai.
    target_state = failover_result.get("target_state", {})
    step(
        4,
        "verify_state_replica",
        target=target,
        weights=target_state.get("weights"),
        vector_count=target_state.get("count", target_state.get("vectors", {}).get("count")),
        embed_model_version=failover_result.get("embed_model_version"),
        rpo_seconds=failover_result.get("rpo_seconds"),
        docs_lost=failover_result.get("docs_lost"),
    )

    # 5. Không cutover lần nữa; xác minh kết quả mà failover.py đã tạo.
    active_file = pathlib.Path("edge/active_region")
    active = active_file.read_text(encoding="utf-8").strip() if active_file.exists() else None
    cutover_ok = active == target
    step(5, "dns_cutover", expected=target, active=active, ok=cutover_ok)
    if not cutover_ok:
        result["error"] = f"active region is {active!r}, expected {target!r}"
        return result

    # 6. Đo 10 request thật trực tiếp trên target để kiểm tra error rate và p95.
    latencies = []
    errors = 0
    samples = []
    for request_no in range(1, 11):
        request_started = time.perf_counter()
        try:
            response = httpx.get(
                f"{URL[target]}/v1/infer",
                params={"q": f"golden signal {request_no}"},
                timeout=3.0,
            )
            ok = response.status_code == 200
            status = response.status_code
            if not ok:
                errors += 1
        except httpx.RequestError as exc:
            ok = False
            status = None
            errors += 1
            samples.append({"request": request_no, "ok": False,
                            "status": None, "error": type(exc).__name__})
        latency_ms = round((time.perf_counter() - request_started) * 1000, 2)
        latencies.append(latency_ms)
        if len(samples) < request_no:
            samples.append({"request": request_no, "ok": ok, "status": status,
                            "latency_ms": latency_ms})
        else:
            samples[-1]["latency_ms"] = latency_ms

    sorted_latencies = sorted(latencies)
    p95_index = max(0, math.ceil(0.95 * len(sorted_latencies)) - 1)
    p95_ms = sorted_latencies[p95_index]
    error_rate = round(errors / len(samples), 3)
    step(6, "verify_golden_signals", requests=len(samples), errors=errors,
         error_rate=error_rate, p95_latency_ms=p95_ms, samples=samples)
    if errors:
        result["error"] = f"golden signals failed: {errors}/{len(samples)} requests"
        return result

    # 7. Kết thúc timeline và ghi lệnh tái lập phép đo.
    elapsed = round(time.time() - started, 3)
    measure_command = (
        "python tools/measure_rto.py --loadgen "
        "reports/drill-2-withdr.jsonl --target-rto 300"
    )
    step(7, "post_incident", elapsed_s=elapsed, measure_command=measure_command)
    result.update(ok=True, elapsed_s=elapsed, p95_latency_ms=p95_ms,
                  error_rate=error_rate)
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2))
