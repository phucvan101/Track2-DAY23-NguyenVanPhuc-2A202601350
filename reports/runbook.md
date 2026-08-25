# Runbook 1 trang — Region chính down

Runbook phải chạy được lúc 3h sáng bởi người KHÔNG viết nó. Mỗi bước: lệnh copy-paste
được + cách biết bước đó xong.

| # | Bước | Lệnh | Biết là xong khi | Ai làm |
|---|---|---|---|---|
| 1 | Xác nhận outage | `python3 chaos/kill_region.py status` | `a.alive=false` VÀ `a.ready=false` 3 lần liên tiếp, cách nhau ≥5s (hoặc: `reports/health-events.jsonl` có dòng mới nhất `region:"a", to:"UNHEALTHY"` — xem `tail -5 reports/health-events.jsonl`) | on-call |
| 2 | Mở incident + bấm giờ RTO | `echo "incident opened $(date -u +%FT%TZ)"` | ts ghi vào `reports/runbook-run.jsonl` (dòng `step:2, name:"thong_bao_incident"`) | on-call |
| 3 | Restore state ở region phụ | `python3 state/snapshot.py get --region b --backend fs` | Lệnh in ra JSON có `restored_at` và `embed_model_version` khớp với region chính; hoặc xem `reports/failover-events.jsonl` có dòng `step:"2_restore_snapshot"` với `rpo_seconds`/`docs_lost` không null | on-call |
| 4 | Scale pool warm→full | `echo full > state/region-b/pool_state` | `curl -s localhost:8002/readyz` của b trả `"ready":true` (HTTP 200) — có thể mất tới `WARMUP_SECONDS` giây | on-call |
| 5 | DNS/LB cutover | `printf b > edge/active_region` | `curl -s localhost:8080/edge/state` cho `active_region:"b"` (đợi tối đa `EDGE_TTL_SECONDS` giây để cache hết hạn) | on-call |
| 6 | Verify golden signals | `for i in $(seq 10); do curl -s -o /dev/null -w "%{http_code} %{time_total}\n" localhost:8002/v1/infer; done` | p95 < `500ms`, error rate < `1%` (xem `reports/runbook-run.jsonl` dòng `step:6, name:"verify_golden_signals"` — có `p95_ms` và `error_rate`) | on-call |
| 7 | Đo RTO + postmortem | `python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | `rto_verdict` != null (mong đợi `"PASS"`); mở incident channel, chia sẻ kết quả, lên lịch viết `reports/postmortem.md` trong 24h | on-call → incident commander |

**Tự động hoá:** các bước 1–7 ở trên được `dr/runbook.py --primary a --target b --backend fs`
thực hiện tự động (bước 1 chờ alert thật từ `dr/health_checker.py`, không tự đoán nhanh
hơn nó — xem docstring trong `dr/runbook.py`). Mặc định script hỏi `y/N` trước khi cutover;
`--auto` chỉ dùng khi chấm điểm / CI, KHÔNG dùng cho incident thật trừ khi đã được duyệt
trước (§4 Anti-Patterns: full-auto không circuit breaker → flapping).

**Rollback (failover ngược):** chỉ trigger rollback về region A khi CẢ HAI điều kiện sau
đều đúng — (1) `curl localhost:8001/readyz` trả 200 ổn định trong ≥ 5 phút liên tục
(không phải một lần), và (2) đã chạy `state/replicate.py` từ B về A (hoặc xác nhận A
không mất dữ liệu nào so với B) để không tạo ra RPO ngược. **Ai quyết định:** Incident
Commander (không phải on-call một mình) — vì rollback quá sớm khi A còn chưa ổn định
chính là kịch bản flapping 2 chiều mà §4 Anti-Patterns cảnh báo. Lệnh rollback:
`python3 dr/runbook.py --primary b --target a --backend fs` sau khi đã xác nhận cả hai
điều kiện trên bằng tay.
