# Postmortem — DR Drill Lab 23

Blameless: câu hỏi là "hệ thống/process nào cho phép chuyện này", không phải "ai làm sai".
Môi trường: mô phỏng 2 region cục bộ (bare mode, `--mock`). Outage là drill chủ động (`netblock` → SIGSTOP Region A).

## 1. Timeline (UTC, mọi dòng có evidence path:line)

| ISO time | Sự kiện | Evidence |
|---|---|---|
| 09:26:49 | outage bắt đầu (Region A bị chặn) | `chaos/chaos-events.jsonl:5` |
| 09:26:49 (+0.04s) | user đầu tiên bị ảnh hưởng | `reports/drill-2-withdr.jsonl:15` |
| 09:27:04 (+15.0s) | health check alert: A UNHEALTHY (3 lần timeout liên tiếp) | `reports/health-events.jsonl:2` |
| 09:27:11 (+22.8s) | operator (runbook `--auto`) confirm, restore snapshot vào B | `reports/failover-events.jsonl:2` |
| 09:27:18 (+29.0s) | B ready, DNS cutover sang B | `reports/failover-events.jsonl:5` |
| 09:27:23 (+34.3s) | resolved: request đầu tiên OK từ region phụ | `reports/drill-2-withdr.jsonl:32` |

## 2. RTO/RPO đo được vs mục tiêu — gap ở bước nào?

- RTO mục tiêu: 300s · đo được: `34.3s` · gap: `265.7s` (đạt, dư 88%)
- RPO mục tiêu: 300s · đo được: `6.01s` (`3` doc bị mất) · gap: `293.99s` (đạt)
- **Bước tốn nhiều giây nhất:** health-check detect (15.0s, 44% RTO) — vì phải đợi `interval × threshold` = 5s × 3 trước khi coi region là chết, để tránh flapping. Đây là giá cố ý chấp nhận.
- Bước đáng ngạc nhiên: runbook mất 7.8s chỉ để "xác nhận lại" outage (`reports/runbook-run.jsonl:1`), do probe tuần tự và mỗi probe chờ hết timeout 2s.

## 3. Root cause (5 whys)

Câu hỏi: nếu đây là outage thật, bước nào trong runbook của tôi sẽ thất bại?

1. **Vì sao user lỗi tới 34s?** Vì sau khi A chết, request vẫn đi vào A cho tới khi edge đổi sang B (+29s) và cache TTL hết (+34.3s).
2. **Vì sao cutover mất 29s?** Vì phải qua chuỗi tuần tự: detect 15s → xác nhận 7.8s → warm-up pool 6.2s.
3. **Vì sao detect mất 15s và xác nhận thêm 7.8s?** Vì health checker cần 3 lần fail liên tiếp mỗi 5s, rồi runbook lại probe lần nữa tuần tự với timeout 2s — hai lớp xác nhận cộng dồn thay vì song song.
4. **Vì sao region B phải warm-up 6.2s và restore từ snapshot?** Vì B ở trạng thái `warm` và rỗng (không có weights, `count=0`); dữ liệu chỉ có ở snapshot định kỳ 30s.
5. **Vì sao thiết kế như vậy?** Vì DR ở mức "pilot light/warm standby" để giảm chi phí, và RPO phụ thuộc chu kỳ replication (30s) chứ không phải đồng bộ liên tục. Nếu đây là outage thật, rủi ro lớn nhất là: snapshot cũ/không tồn tại (bước 2 chết), B không bao giờ ready (bước 4 abort), và probe tuần tự làm chậm xác nhận khi mạng treo.

## 4. Action items

| # | Action | Owner | Deadline | Giảm RTO/RPO bao nhiêu giây |
|---|---|---|---|---|
| 1 | Probe 2 region song song, timeout 1s trong `dr/runbook.py` bước 1 | Nguyễn Khắc Giáp (dev) | 2026-10-16 | Giảm ~6.8s RTO (7.8s → ~1s) |
| 2 | Hạ `EDGE_TTL_SECONDS` từ 5s xuống 2s | Nguyễn Khắc Giáp (infra) | 2026-10-16 | Giảm ~3s RTO |
| 3 | Giữ pool B ở warm-standby nóng hơn (pre-load weights) | Trưởng nhóm hạ tầng | 2026-10-30 | Giảm tới ~6s RTO (warm-up) |
| 4 | Hạ chu kỳ replication 30s → 10s | Nguyễn Khắc Giáp (data) | 2026-10-23 | Giảm RPO từ tối đa ~30s xuống ~10s |
| 5 | Cân nhắc interval 2s × threshold 3 sau khi có circuit breaker chống flapping | Trưởng nhóm SRE | 2026-11-06 | Giảm ~9s RTO (floor 15s → 6s) |

## 5. Ba câu hỏi bắt buộc trả lời

1. **`interval × threshold` bao nhiêu giây? Chiếm bao nhiêu % RTO?** 5s × 3 = 15s, chiếm 15.0 / 34.3 ≈ 44% RTO. Đây là sàn: không thể phát hiện nhanh hơn 15s với cấu hình này (test `test_health_check_interval_duoc_ghi_lai` kiểm tra điều này).
2. **Hạ interval xuống 1s thì RTO giảm mấy giây, trả giá gì?** Floor còn 1s × 3 = 3s, giảm 12s (RTO còn ~22.3s). Giá phải trả: gấp 5 lần số probe, và dễ **flapping** — một lần chập chờn mạng ngắn vài giây cũng có thể kích hoạt failover và failover ngược liên tục (§4 Anti-Patterns). Vì vậy runbook giữ bước confirm của người và không full-auto.
3. **Nếu outage kéo dài 6 giờ và region chính mất dữ liệu vĩnh viễn, `docs_lost` có nghĩa gì với khách hàng?** `docs_lost = 3` là số document ghi sau snapshot cuối nên mất vĩnh viễn: khách hàng phải gửi lại hoặc bị trả kết quả thiếu những dữ liệu đó. Nếu A mất vĩnh viễn thì không còn nguồn nào khôi phục 3 doc này; nếu outage dài và ingest vẫn chạy trên B thì mất thêm các ghi chưa replicate ngược. Vì thế RPO phải được báo cho khách hàng bằng số doc cụ thể, và cần replication thường xuyên hơn cho dữ liệu quan trọng.
