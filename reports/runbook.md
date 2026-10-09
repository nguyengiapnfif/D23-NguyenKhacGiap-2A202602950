# Runbook 1 trang — Region chính down

Chạy được lúc 3h sáng bởi người không viết nó. Chạy mọi lệnh từ thư mục gốc repo,
với `.venv` đã kích hoạt (`source .venv/bin/activate`). Bán tự động: có alert + confirm của người, tránh flapping.

| # | Bước | Lệnh | Biết là xong khi | Ai làm | Rollback / dừng nếu |
|---|---|---|---|---|---|
| 1 | Xác nhận outage (không tin 1 lần fail) | `python chaos/kill_region.py status` và `curl -s localhost:8001/readyz` (lặp 3 lần) | `a.alive=false` / `/readyz` của A lỗi 3 lần liên tiếp; `reports/health-events.jsonl` có `to:UNHEALTHY, region:a` | on-call | Chỉ 1 lần fail → KHÔNG failover, probe tiếp |
| 2 | Mở incident + bấm giờ RTO | `python dr/runbook.py --primary a --target b --backend fs` (hỏi y/N; thêm `--auto` chỉ trong CI/chấm điểm) | dòng `thong_bao_incident` trong `reports/runbook-run.jsonl` | on-call | Trả lời `N` → dừng, không đổi gì |
| 3 | Restore state ở region phụ | tự động trong runbook (`python state/snapshot.py get --region b --backend fs`) | `reports/failover-events.jsonl` có `2_restore_snapshot` với `rpo_seconds`, `docs_lost` | on-call | Không có `state/_replica/.../MANIFEST.json` → dừng, chạy `python state/snapshot.py put --region a --backend fs` nếu A còn đọc được, hoặc báo trưởng nhóm |
| 4 | Scale pool warm→full, chờ ready | tự động (`pool_state` = `full`); kiểm tra `curl -s localhost:8002/readyz` | `/readyz` của B trả 200 (`4_wait_ready ok:true`) | on-call | Quá 60s chưa ready → failover tự ABORT, **không cutover**; điều tra `run/region-b.log` |
| 5 | DNS/LB cutover | tự động; kiểm tra `curl -s localhost:8080/edge/state` | `active_region=b` (có thể trễ tới TTL 5s) | on-call | Chỉ cutover sau bước 4; nếu B bắt đầu lỗi → xem mục Rollback |
| 6 | Verify golden signals | tự động: 10 request vào B | p95 < 500ms và error rate = 0 (10/10 OK) | on-call | error rate > 0 → giữ incident mở, escalate |
| 7 | Đo RTO + postmortem | `python tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | `rto_verdict` = `PASS`/`FAIL` (khác `null`) | on-call + trưởng nhóm | Viết `reports/postmortem.md` trong 48h |

## Rollback (failover ngược về region A)

- **Điều kiện:** A phục hồi (`/readyz` 200 liên tục ≥ 3 lần probe, ≥ 5 phút ổn định) **và** dữ liệu đã đồng bộ ngược từ B sang A (không mất doc ghi trong lúc A chết).
- **Ai quyết định:** trưởng nhóm / incident commander, không tự động. Mọi failover ngược đều qua confirm của người (full-auto không có circuit breaker → hai region flap qua lại).
- **Cách làm:** `python dr/runbook.py --primary b --target a --backend fs` sau khi đã `put` snapshot từ B.
