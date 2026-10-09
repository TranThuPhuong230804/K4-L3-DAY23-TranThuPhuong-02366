# RTO/RPO Evidence — Lab 23

Tất cả số liệu dưới đây được lấy từ drill chạy thật ngày 2026-10-09. Timestamp trong
JSONL là UTC; phép đo dùng epoch `ts` để không phụ thuộc múi giờ hiển thị.

## 1. Drill 1 — không có DR

| Chỉ số | Giá trị | Cách đo | Evidence |
|---|---:|---|---|
| t_outage | `2026-10-09T11:56:59Z` | Chaos `netblock` Region A | `chaos/chaos-events.jsonl:11` |
| Request fail đầu tiên | `+0.01s` | Dòng `ok:false` đầu tiên sau t_outage | `reports/drill-1-nodr.jsonl:17` |
| Request thành công sau lỗi | Không có | 14 request cuối đều lỗi | `reports/drill-1-nodr.jsonl:30` |
| RTO | `NO_RECOVERY` | Không có `ok:true` sau failure | `reports/drill-1-nodr.jsonl:30` |

Baseline có 30 request, trong đó 14 request thất bại. Edge tiếp tục trỏ vào A cho đến
hết cửa sổ đo, chứng minh hệ thống không tự phục hồi khi chưa có DR automation.

## 2. Drill 2 — có DR

| Mốc | Giây từ t_outage | Cách đo | Evidence |
|---|---:|---|---|
| t_outage | `0.0s` | Chaos `netblock` Region A | `chaos/chaos-events.jsonl:9` |
| User thấy lỗi đầu tiên | `+0.4s` | Dòng `ok:false` đầu tiên | `reports/drill-2-withdr.jsonl:26` |
| Health checker phát hiện A unhealthy | `+16.1s` | 3 lần fail liên tiếp, interval 5s | `reports/health-events.jsonl:2` |
| Snapshot restore hoàn tất | `+13.0s` | `2_restore_snapshot` | `reports/failover-events.jsonl:2` |
| Region B ready | `+19.2s` | `/readyz=200` sau warm-up | `reports/failover-events.jsonl:4` |
| DNS/LB cutover sang B | `+19.2s` | `active=b` | `reports/failover-events.jsonl:5` |
| **Request đầu tiên phục hồi từ B** | **`+22.8s`** | `ok:true`, `served_by:b` | `reports/drill-2-withdr.jsonl:36` |

| Chỉ số | Đo được | Mục tiêu | Verdict | Evidence |
|---|---:|---:|---|---|
| RTO — Inference API | **22.8s** | 300s | **PASS** | `reports/drill-2-withdr.jsonl:36` |
| RPO — Vector DB | **29.86s / 14 docs** | 300s | **PASS** | `reports/failover-events.jsonl:2` |

Drill có 153 request; 10 request thất bại sau outage. Kết quả phục hồi do Region B
phục vụ, không phải Region A vừa bị kill.

## 3. Phân rã RTO

Các phase restore và warm-up bắt đầu trong lúc health checker vẫn đang tích lũy
threshold, nên raw duration có overlap và không được cộng thẳng hai lần.

| Thành phần | Raw duration | Đóng góp trên critical path | Evidence | Cách giảm |
|---|---:|---:|---|---|
| Health-check detect floor | `15.0s` floor; `16.1s` thực tế | `16.1s` | `reports/health-events.jsonl:2` | Giảm interval nhưng giữ threshold/circuit breaker |
| Snapshot restore | `0.09s` | `0.0s` sau detect vì đã hoàn tất ở +13.0s | `reports/failover-events.jsonl:1`, `reports/failover-events.jsonl:2` | Snapshot thường xuyên hơn; giữ bản sao nóng |
| GPU pool warm-up | `6.181s` | `3.1s` còn lại sau detect | `reports/failover-events.jsonl:3`, `reports/failover-events.jsonl:4` | Duy trì warm capacity hoặc pre-warm |
| DNS/LB TTL cache | `3.6s` | `3.6s` | `reports/failover-events.jsonl:5`, `reports/drill-2-withdr.jsonl:36` | Giảm TTL có kiểm soát |

Critical path: `16.1s + 3.1s + 3.6s = 22.8s`.

Replication chạy mỗi 30 giây; snapshot dùng để restore được ghi tại
`reports/replication.jsonl:1`. Model version `embed-model=vi-e5-base@v3` được phục
hồi cùng vector DB, tránh lỗi index/model không tương thích.
