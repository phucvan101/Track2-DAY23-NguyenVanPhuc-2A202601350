# RTO/RPO Evidence — Lab 23

Quy tắc duy nhất: mỗi con số ở đây phải trỏ được về **một dòng log thật**
(`đường/dẫn.jsonl:số_dòng`). `pytest tests/test_rto_evidence.py` sẽ mở từng file ra kiểm tra.
Con số không có evidence = trượt, bất kể các phần khác.

## 1. Drill 1 — không có DR (baseline)

t_outage = `2026-08-25T09:23:17` (`chaos/chaos-events.jsonl:1`).

| Chỉ số | Giá trị | Cách đo | Evidence |
|---|---|---|---|
| t_outage | `2026-08-25T09:23:17` | chaos kill | `chaos/chaos-events.jsonl:1` |
| Request fail đầu tiên | `+0.1s` | dòng `ok:false` đầu tiên sau t_outage | `reports/drill-1-nodr.jsonl:17` |
| Request thành công sau đó | không có | không có dòng `ok:true` nào sau t_outage trong 32 request | `reports/measure-drill-1.json` |
| RTO | `NO_RECOVERY` | `tools/measure_rto.py` | `reports/measure-drill-1.json` |

Region A không hề tự phục hồi trong toàn bộ 40s của drill — 16/32 request thất bại
(`reports/measure-drill-1.json`), không có bất kỳ dòng `ok:true` nào sau `t_outage`.
Đây chính là baseline "không có DR" phải chứng minh trước khi viết `dr/`.

## 2. Drill 2 — có DR

t_outage (mốc 0) = `2026-08-25T09:24:18` (`chaos/chaos-events.jsonl:3`).

| Mốc | +giây từ t_outage | Cách đo | Evidence |
|---|---|---|---|
| t_outage (mốc 0) | 0.0s | `action:kill` | `chaos/chaos-events.jsonl:3` |
| User thấy lỗi đầu tiên | +0.1s | dòng `ok:false` đầu | `reports/drill-2-withdr.jsonl:25` |
| Health check phát hiện | +19.2s | `to:UNHEALTHY, region:a` | `reports/health-events.jsonl:2` |
| Snapshot restore xong | +19.4s | `step:2_restore_snapshot` | `reports/failover-events.jsonl:2` |
| Region phụ ready | +25.8s | `step:4_wait_ready` | `reports/failover-events.jsonl:4` |
| DNS cutover | +25.8s | `step:5_dns_cutover` | `reports/failover-events.jsonl:5` |
| **RTO đo được** | **+28.4s** | dòng `ok:true` đầu sau lỗi, `served_by:"b"` | `reports/drill-2-withdr.jsonl:39` |

| Chỉ số | Đo được | Mục tiêu (slide §1) | Verdict |
|---|---|---|---|
| RTO — Inference API | `28.4s` | 300s (5 phút) | **PASS** |
| RPO — Vector DB | `8.02s` / `4` doc | 300s (5 phút) | **PASS** |

Toàn bộ số ở trên do `tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300`
tính ra (`reports/measure-drill-2.json`): `"valid": true`, `"warnings": []`,
`"rto_measured_s": 28.4`, `"rto_verdict": "PASS"`, `"rpo_at_restore_s": 8.02`, `"docs_lost": 4`.

## 3. RTO của tôi gồm những gì (bắt buộc — đây là phần chấm điểm hiểu bài)

| Thành phần | Giây | Nó đến từ đâu | Giảm được bằng cách nào |
|---|---|---|---|
| Health-check detect floor | `15.0s` | `interval_s(5.0) × threshold(3)` trong `reports/health-events.jsonl:2` | Hạ `interval` hoặc `threshold` — đổi lại tăng rủi ro flapping (§4 Anti-Patterns) nếu region chỉ rớt tạm thời |
| Snapshot restore | `~0.2s` | delta giữa `1_verify_target` và `2_restore_snapshot` trong `reports/failover-events.jsonl:1`–`reports/failover-events.jsonl:2` — backend `fs` copy file cục bộ nên gần như tức thời | Với backend `minio` thật (mạng, không phải copy file cục bộ) sẽ chậm hơn — đo lại nếu dùng stretch goal MinIO |
| GPU pool warm-up | `6.32s` | `waited_s` ở `reports/failover-events.jsonl:4` (`step:4_wait_ready`) | Giảm `WARMUP_SECONDS` — đánh đổi: pool chưa thật sự "nóng" khi nhận traffic thật, tăng rủi ro latency spike |
| DNS/LB TTL cache | `2.6s` | `t_recovered(28.4s) − t_cutover(25.8s)` = `reports/drill-2-withdr.jsonl:39` trừ `reports/failover-events.jsonl:5` | Giảm `EDGE_TTL_SECONDS` — đánh đổi: DNS/LB phải resolve lại thường xuyên hơn, tăng tải lên nguồn DNS |

Tổng 4 thành phần: `15.0 + 0.2 + 6.32 + 2.6 ≈ 24.1s`, thấp hơn RTO đo được (`28.4s`) khoảng `4.3s`
— chênh lệch này là phần **health checker mất nhiều hơn floor lý thuyết** (đo thực tế
`19.2s` thay vì `15.0s`), vì: (1) `kill` rơi giữa chu kỳ poll 5s nên lần fail đầu tiên
không xảy ra ngay giây 0, và (2) mỗi lần probe trong 3 lần liên tiếp dùng timeout 2s
(`netblock` = `SIGSTOP` → request treo tới timeout), cộng dồn vào thời gian phát hiện.
Phân tích đầy đủ ở `reports/postmortem.md` mục 2.
