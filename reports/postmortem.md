# Postmortem — DR Drill Lab 23

Ngày diễn tập: 2026-10-09. Đây là postmortem blameless: phân tích tập trung vào cơ
chế hệ thống và quy trình, không quy lỗi cho người vận hành.

## 1. Timeline

Timestamp là UTC theo log JSONL.

| ISO time | Sự kiện | Evidence |
|---|---|---|
| 2026-10-09T11:52:52.997Z | Outage bắt đầu: Region A bị `netblock` | `chaos/chaos-events.jsonl:9` |
| 2026-10-09T11:52:53.372Z | User đầu tiên nhận 503/ReadTimeout | `reports/drill-2-withdr.jsonl:26` |
| 2026-10-09T11:53:05.816Z | Operator xác nhận sau ba probe và duyệt failover | `reports/runbook-run.jsonl:2` |
| 2026-10-09T11:53:09.144Z | Health checker chuyển A sang UNHEALTHY | `reports/health-events.jsonl:2` |
| 2026-10-09T11:53:12.196Z | B ready và DNS/LB cutover hoàn tất | `reports/failover-events.jsonl:5` |
| 2026-10-09T11:53:15.813Z | Request đầu tiên thành công từ Region B | `reports/drill-2-withdr.jsonl:36` |

## 2. RTO/RPO so với mục tiêu và gap analysis

- RTO mục tiêu: 300s; đo được: **22.8s**; headroom/gap: **277.2s**.
- RPO mục tiêu: 300s; đo được: **29.86s / 14 docs**; headroom/gap:
  **270.14s**. Evidence: `reports/failover-events.jsonl:2`.
- Phase lớn nhất là health detection: **16.1s**, tương đương 70.6% RTO. Detect
  floor cấu hình là 15s; 1.1s còn lại đến từ lịch poll và timeout.
- Restore và GPU warm-up overlap với cửa sổ detection. Sau detection chỉ còn 3.1s
  chờ B ready/cutover, rồi 3.6s cho Edge hết TTL cache.

## 3. Root cause — 5 whys

1. User lỗi vì Edge vẫn gửi request tới A sau khi A ngừng phản hồi.
2. Edge chỉ đọc `active_region`; nó không tự quyết định failover từ liveness/readiness.
3. Chưa thể chuyển ngay sang B vì B ban đầu ở trạng thái warm, thiếu weights và vectors.
4. State, compute readiness và traffic routing là ba lớp riêng; cả ba phải hoàn tất đúng
   thứ tự restore → scale → ready → cutover.
5. Baseline không có health checker/runbook nên không có cơ chế đóng vòng từ phát hiện
   tới phục hồi. Automation mới đã đóng vòng này và ngăn cutover trước readiness.

Nếu đây là outage thật, rủi ro lớn nhất là snapshot bị thiếu hoặc quá cũ: bước
`2_restore_snapshot` sẽ abort và không có B đủ state để phục vụ. Việc log model version,
RPO và docs lost là guardrail bắt buộc.

## 4. Action items

| # | Action item | Owner | Deadline | Tác động dự kiến |
|---|---|---|---|---|
| 1 | Thử interval 2s, threshold 3 và thêm circuit breaker/alert dedup | SRE | 2026-10-16 | Detect floor 15s → 6s, giảm tối đa 9s RTO |
| 2 | Duy trì replica có weights/vector mới nhất và một pool B pre-warm tối thiểu | AI Platform | 2026-10-23 | Giảm khoảng 6.2s warm-up và giảm rủi ro restore |
| 3 | Cảnh báo replication lag >60s và kiểm tra restore tự động hằng ngày | Data Platform | 2026-10-23 | Giới hạn RPO và phát hiện snapshot hỏng trước incident |

## 5. Câu hỏi bắt buộc

1. `interval × threshold = 5s × 3 = 15s`, chiếm **65.8%** của RTO 22.8s.
   Detection thực tế 16.1s chiếm 70.6%.
2. Nếu interval giảm xuống 1s và vẫn giữ threshold 3, detect floor còn 3s — giảm tối
   đa 12s, RTO lý thuyết còn khoảng 10.8s nếu các phase khác không đổi. Đổi lại là số
   probe tăng 5 lần, nhạy hơn với network jitter và tăng nguy cơ flapping/false alert;
   cần circuit breaker và alert dedup.
3. Trong drill, 14 docs lost là các document primary đã nhận sau snapshot nhưng bản
   restore ở B chưa có. Với outage 6 giờ và primary mất dữ liệu vĩnh viễn, đây là giao
   dịch/ticket của khách hàng cần replay từ nguồn sự kiện hoặc đối soát; nếu không có
   nguồn replay thì đó là mất dữ liệu thực, phải thông báo theo quy trình incident.
