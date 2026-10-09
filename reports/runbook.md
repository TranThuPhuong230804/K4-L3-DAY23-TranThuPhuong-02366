# Runbook — Region chính down

Phạm vi: bare mode, primary A, secondary B, filesystem snapshot. Primary path là
runbook bán tự động; các lệnh fallback chỉ dùng khi automation dừng đúng tại bước đó.

| # | Bước | Lệnh copy-paste | Hoàn tất khi | Owner |
|---|---|---|---|---|
| 1 | Xác nhận outage | `python chaos/kill_region.py status` | A không ready qua 3 probe liên tiếp; B vẫn alive | SRE on-call |
| 2 | Mở incident và xác nhận failover | `python dr/runbook.py --primary a --target b --backend fs` | Operator nhập `y`; `reports/runbook-run.jsonl` có `thong_bao_incident` | Incident commander |
| 3 | Restore state ở B — fallback | `python state/snapshot.py get --region b --backend fs` | Vector DB, weights và VERSION tồn tại; RPO được ghi | DR operator |
| 4 | Scale và chờ B ready — fallback | `printf 'full\n' > state/region-b/pool_state && until curl -sf localhost:8002/readyz; do sleep 1; done` | `/readyz` của B trả HTTP 200 | Platform on-call |
| 5 | Cutover — fallback, chỉ sau bước 4 | `printf 'b\n' > edge/active_region && sleep 6 && curl -s localhost:8080/edge/state` | `active_region` là `b`; không cutover nếu B chưa ready | Incident commander |
| 6 | Verify golden signals | `for i in $(seq 1 10); do curl -sf localhost:8080/v1/infer; done` | 10/10 request thành công từ B, error rate <1%, p95 <500ms | SRE on-call |
| 7 | Đo RTO/RPO và mở postmortem | `python tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | `valid:true`, không warning, `rto_verdict:PASS` | Incident commander |

Lần diễn tập này hoàn tất cả bảy bước tại `reports/runbook-run.jsonl:7`; golden
signals đạt error rate 0% và p95 303.99ms tại `reports/runbook-run.jsonl:6`.

## Abort và rollback

- Abort trước cutover nếu restore thiếu weights/model version, vector count bằng 0,
  hoặc `/readyz` của B không đạt 200 trong 60 giây. Khi abort, giữ
  `edge/active_region` ở A và escalation cho Platform on-call.
- Không failback tự động. Chỉ Incident commander được duyệt trả traffic về A sau khi
  A trả `/readyz=200` ba lần liên tiếp, dữ liệu phát sinh trên B đã được reconcile,
  golden signals đạt error rate <1% và p95 <500ms trong ít nhất 15 phút.
- Failback có circuit breaker 30 phút để tránh A/B flapping. Sau khi được duyệt:
  `printf 'a\n' > edge/active_region && sleep 6 && curl -s localhost:8080/edge/state`.
- Nếu golden signals xấu sau failback, trả ngay về B bằng
  `printf 'b\n' > edge/active_region` và cập nhật incident timeline.
