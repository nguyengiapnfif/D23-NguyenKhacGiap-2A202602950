# RTO/RPO Evidence — Lab 23

Mỗi con số trỏ về một dòng log thật (`đường/dẫn:số_dòng`). Đo bằng `tools/measure_rto.py`
(kết quả đầy đủ: `reports/measure-drill-1.json`, `reports/measure-drill-2.json`).

> Ghi chú: `chaos/chaos-events.jsonl` dòng 3–4 là lần chạy drill 2 đầu tiên, bị `measure_rto`
> cảnh báo "cutover trước khi health check phát hiện" (runbook chưa chờ alert). Tôi sửa
> `dr/runbook.py` để bước 1 chờ health checker rồi chạy lại; số liệu dưới đây là lần chạy lại (dòng 5–6).

## 1. Drill 1 — không có DR (baseline)

| Chỉ số | Giá trị | Cách đo | Evidence |
|---|---|---|---|
| t_outage | `2026-10-09T09:22:13` | chaos kill | `chaos/chaos-events.jsonl:1` |
| Request fail đầu tiên | +0.07s | dòng `ok:false` đầu tiên sau t_outage | `reports/drill-1-nodr.jsonl:17` |
| Request thành công sau đó | không có (16/32 request fail, không bao giờ hết) | không có dòng `ok:true` nào sau t_outage | `reports/measure-drill-1.json` |
| RTO | `NO_RECOVERY` | `tools/measure_rto.py` | `reports/measure-drill-1.json` |

## 2. Drill 2 — có DR

| Mốc | +giây từ t_outage | Cách đo | Evidence |
|---|---|---|---|
| t_outage (mốc 0) | 0s (`2026-10-09T09:26:49`) | `action:kill` | `chaos/chaos-events.jsonl:5` |
| User thấy lỗi đầu tiên | +0.04s | dòng `ok:false` đầu | `reports/drill-2-withdr.jsonl:15` |
| Health check phát hiện | +15.0s | `to:UNHEALTHY, region:a` | `reports/health-events.jsonl:2` |
| Snapshot restore xong | +22.8s | `step:2_restore_snapshot` | `reports/failover-events.jsonl:2` |
| Region phụ ready | +29.0s | `step:4_wait_ready` | `reports/failover-events.jsonl:4` |
| DNS cutover | +29.0s | `step:5_dns_cutover` | `reports/failover-events.jsonl:5` |
| **RTO đo được** | **+34.3s** | dòng `ok:true` đầu sau lỗi (served_by=b) | `reports/drill-2-withdr.jsonl:32` |

| Chỉ số | Đo được | Mục tiêu (slide §1) | Verdict |
|---|---|---|---|
| RTO — Inference API | `34.3s` | 300s (5 phút) | PASS (nhanh hơn mục tiêu ~8.7 lần) |
| RPO — Vector DB | `6.01s` / `3` doc | 300s (5 phút) | PASS |

RPO lấy từ `rpo_seconds`/`docs_lost` tại `reports/failover-events.jsonl:2` (replication mỗi 30s, `reports/replication.jsonl`).

## 3. RTO của tôi gồm những gì

| Thành phần | Giây | Nó đến từ đâu | Giảm được bằng cách nào |
|---|---|---|---|
| Health-check detect floor | 15.0s | `interval_s × threshold` = 5 × 3, xem `reports/health-events.jsonl:2` | Hạ interval (1s → floor 3s) đổi lấy nguy cơ flapping và nhiều probe hơn |
| Runbook xác nhận lại outage | 7.8s | bước 1 probe tuần tự 3 lần, mỗi lần chờ hết timeout 2s vì `netblock` làm request treo (`reports/runbook-run.jsonl:1`) | Probe song song, timeout 1s → còn ~1s |
| Snapshot restore | 0.0s | `2_restore` → `3_scale` (copy file nhỏ, `reports/failover-events.jsonl:2-3`) | Không đáng kể ở quy mô lab; thật sự sẽ tăng theo dung lượng weights/index |
| GPU pool warm-up | 6.2s | `waited_s` ở `4_wait_ready` (`reports/failover-events.jsonl:4`, WARMUP_SECONDS=6) | Giữ pool ở trạng thái warm-standby nóng hơn / pre-load weights |
| DNS/LB TTL cache | 5.3s | t_recovered − t_cutover (+34.3 − +29.0), edge cache `EDGE_TTL_SECONDS=5` | Hạ TTL xuống 1–2s |

Cộng lại: 15.0 + 7.8 + 0.0 + 6.2 + 5.3 = 34.3s, khớp RTO đo được.
