# Postmortem — DR Drill Lab 23

Theo đúng template §4 "Sau Failover: Blameless Postmortem". Blameless: câu hỏi là
"hệ thống/process nào cho phép chuyện này", không phải "ai làm sai".

## 1. Timeline (mọi dòng phải có evidence path:line)

| ISO time | Sự kiện | Evidence |
|---|---|---|
| `2026-08-25T09:24:18` | outage bắt đầu (`kill --region a --mode netblock --mock`) | `chaos/chaos-events.jsonl:3` |
| `2026-08-25T09:24:18` (+0.1s) | user đầu tiên bị ảnh hưởng — `503 ReadTimeout` | `reports/drill-2-withdr.jsonl:25` |
| `2026-08-25T09:24:37` (+19.2s) | health check alert — `region:a, to:UNHEALTHY` | `reports/health-events.jsonl:2` |
| `2026-08-25T09:24:37` (+19.3s) | operator (runbook `--auto`) nhận alert và xác nhận cutover | `reports/runbook-run.jsonl:2` |
| `2026-08-25T09:24:43` (+25.8s) | DNS cutover sang region b | `reports/failover-events.jsonl:5` |
| `2026-08-25T09:24:46` (+28.4s) | resolved — request đầu tiên OK từ region phụ (`served_by:"b"`) | `reports/drill-2-withdr.jsonl:39` |

## 2. RTO/RPO đo được vs mục tiêu — gap ở bước nào?

- RTO mục tiêu: 300s · đo được: `28.4s` · gap: `-271.6s` (đạt mục tiêu, còn dư nhiều biên độ)
- RPO mục tiêu: 300s · đo được: `8.02s` (`4` doc bị mất) · gap: `-291.98s` (đạt mục tiêu)
- **Bước tốn nhiều giây nhất:** `health-check detection (19.2s / 28.4s ≈ 68%)` — vì health
  checker chỉ được phép tin một outage sau `threshold=3` lần fail liên tiếp cách nhau
  `interval=5s`, tức sàn lý thuyết đã là `15.0s` (`reports/health-events.jsonl:2`). Thực tế
  đo được `19.2s`, cao hơn sàn `4.2s`, vì hai lý do: (1) `kill` xảy ra giữa một chu kỳ poll
  5s (không phải đúng lúc health checker vừa poll xong), nên "giây 0" của health checker
  trễ hơn `t_outage` thật; (2) chế độ `netblock` = `SIGSTOP` khiến mỗi lần probe phải chờ
  hết `timeout=2s` mới biết là fail (không fail nhanh như `ConnectError`), cộng dồn vào cả
  3 lần probe liên tiếp. Bước tốn giây thứ nhì là GPU pool warm-up (`6.32s`,
  `reports/failover-events.jsonl:4`) — không giảm được nếu không hạ `WARMUP_SECONDS`.

## 3. Root cause (5 whys)

Không phải "vì tôi chạy chaos script". Câu hỏi: *nếu đây là outage thật, bước nào
trong runbook của tôi sẽ thất bại?*

1. Tại sao user thấy lỗi? — Vì region A ngừng trả lời (`netblock`/`SIGSTOP`) và edge proxy
   vẫn đang route traffic tới nó (`edge/active_region` chưa đổi).
2. Tại sao edge vẫn route tới region đã chết? — Vì không có cơ chế phát hiện outage tự
   động chạy sẵn *trước khi* outage xảy ra — hệ thống chỉ biết một region "readyz" hay
   không khi có ai chủ động poll nó.
3. Tại sao phải mất tới ~19s mới phát hiện? — Vì thiết kế cố ý chống flapping: một lần
   fail không đủ để kết luận outage (§4 Anti-Patterns), nên cần `threshold=3` lần liên
   tiếp cách nhau `interval=5s` mới được tin — đây là đánh đổi có chủ đích giữa tốc độ
   phát hiện và rủi ro failover nhầm vì một request timeout ngẫu nhiên.
4. Tại sao mất thêm ~9s nữa sau khi phát hiện mới phục hồi hoàn toàn? — Vì cutover không
   được phép xảy ra trước khi region phụ thật sự sẵn sàng (`4_wait_ready` phải đợi tới khi
   `/readyz` trả 200) — region B cần warm-up GPU pool (`WARMUP_SECONDS`) trước khi nhận
   traffic thật, nếu không sẽ tạo ra double-outage (cả hai region đều 503).
5. Tại sao region B không sẵn sàng sẵn từ đầu (để không mất 6.32s warm-up)? — Vì đây là
   kiến trúc active-passive (`pool_state=warm`, không phải `full`) để tiết kiệm chi phí GPU
   khi region phụ không phục vụ traffic — root cause thật sự không phải một lỗi, mà là một
   đánh đổi kiến trúc (cost vs. RTO) được ghi nhận rõ trong §2 Active-Passive vs Active-Active.

## 4. Action items (có owner + deadline)

| # | Action | Owner | Deadline | Giảm RTO/RPO bao nhiêu giây |
|---|---|---|---|---|
| 1 | Chạy `state/replicate.py` liên tục (không phải chỉ trong giờ lab) với `--every 30` hoặc thấp hơn để giảm RPO trung bình | On-call / Platform | 2026-09-01 | Giảm RPO trung bình còn ~15s (một nửa chu kỳ 30s) |
| 2 | Đánh giá hạ `interval` health check xuống 2–3s cho riêng đường dẫn suy luận AI (không đổi cho các dịch vụ khác) sau khi đo tỉ lệ false-positive trong 1 tuần | SRE lead | 2026-09-08 | Có thể giảm detect floor từ 15s xuống 6–9s, đổi lại tăng rủi ro flapping nếu chưa kiểm chứng |
| 3 | Giữ region B ở `pool_state=full` thường trực (active-active một phần) cho giờ cao điểm để loại bỏ 6.32s GPU warm-up | Infra owner | 2026-09-15 | Loại bỏ ~6.3s warm-up khỏi RTO, đổi lại tốn thêm chi phí GPU 24/7 |

## 5. Ba câu hỏi bắt buộc trả lời

1. `interval × threshold` của tôi là `5s × 3 = 15.0s` — đây là **68%** của RTO đo được
   (`15.0 / 22.06` theo baseline floor, hoặc `19.2 / 28.4 ≈ 68%` nếu tính bằng số đo thực
   tế của lần phát hiện này). Đây là thành phần lớn nhất trong toàn bộ RTO.
2. Nếu hạ `interval` xuống `1s` (giữ `threshold=3`), detect floor giảm từ `15.0s` xuống
   `3.0s`, tức RTO có thể giảm khoảng `12s`, còn khoảng `~16s`. Cái giá phải trả (§4
   Anti-Patterns): với `interval=1s`, chỉ cần 3 request bị timeout/chậm liên tiếp trong
   3 giây (network jitter, GC pause, một request chậm bất thường) là đủ để trigger một
   failover không cần thiết — tăng đáng kể rủi ro **flapping** giữa hai region, mỗi lần
   flap lại tốn thêm RTO thật cho các request đang phục vụ dở dang.
3. Nếu outage kéo dài 6 giờ và region chính mất dữ liệu vĩnh viễn, `docs_lost` (đo được là
   `4` document trong lần drill này, tương ứng `8.02s` dữ liệu chưa kịp replicate) sẽ
   không còn là "vài giây log" nữa — với chu kỳ replicate 30s, một outage vĩnh viễn tại
   đúng thời điểm restore nghĩa là **tối đa 30s dữ liệu ingest gần nhất bị mất vĩnh viễn**,
   không phải một con số cố định — nó là hàm của tần suất replicate. Với khách hàng, đó là
   các hoá đơn/ticket họ vừa tạo ngay trước outage biến mất khỏi hệ thống tra cứu, có thể
   phải nhập lại thủ công hoặc đối chiếu bằng nguồn khác.
