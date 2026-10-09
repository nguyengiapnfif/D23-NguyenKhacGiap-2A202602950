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
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def step(n, name, **kw):
    """Ghi 1 dòng {ts, iso, step, name, ...} vào LOG."""
    now = time.time()
    rec = {"ts": now, "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)),
           "step": n, "name": name, **kw}
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    print(json.dumps(rec), flush=True)
    return rec


def confirm(auto: bool, msg: str) -> bool:
    """auto=True -> True; ngược lại hỏi y/N."""
    if auto:
        return True
    return input(f"{msg} [y/N] ").strip().lower() in ("y", "yes")


def _ready(region: str) -> bool:
    try:
        return httpx.get(f"{URL[region]}/readyz", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


def _last_outage_ts():
    p = pathlib.Path("chaos/chaos-events.jsonl")
    if not p.exists():
        return None
    kills = [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
    kills = [e for e in kills if e.get("action") == "kill"]
    return kills[-1]["ts"] if kills else None


def _wait_unhealthy(region: str, since, timeout: float) -> bool:
    """Chờ health checker ghi state_change UNHEALTHY của region sau t_outage."""
    health = pathlib.Path("reports/health-events.jsonl")
    end = time.time() + timeout
    while time.time() < end:
        if health.exists():
            for l in health.read_text().splitlines():
                try:
                    e = json.loads(l)
                except ValueError:
                    continue
                if (e.get("event") == "state_change" and e.get("to") == "UNHEALTHY"
                        and e.get("region") == region and e["ts"] >= (since or 0)):
                    return True
        time.sleep(1)
    return False


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    """7 bước runbook."""
    t_start = time.time()
    out = {"ok": False, "primary": primary, "target": target}

    # 1. xác nhận outage: chờ health checker báo UNHEALTHY (alert), rồi probe lại,
    #    không tin 1 lần fail. Không có health log -> chỉ dựa vào probe.
    t_outage = _last_outage_ts()
    alerted = _wait_unhealthy(primary, t_outage, timeout=60)
    fails = 0
    for _ in range(3):
        fails += 0 if _ready(primary) else 1
        time.sleep(0.5)
    outage = fails == 3
    step(1, "xac_nhan_outage", health_alert=alerted, primary_ready_fails=fails, probes=3,
         target_ready=_ready(target), confirmed=outage)
    if not outage:
        out["error"] = "primary_still_ready"
        return out

    # 2. thông báo incident: mốc operator biết tin (sau t_outage)
    rec = step(2, "thong_bao_incident", t_outage=t_outage,
               notify_delay_s=None if t_outage is None else round(time.time() - t_outage, 2))
    if not confirm(auto, f"Failover region-{primary} -> region-{target}?"):
        step(2, "operator_tu_choi")
        out["error"] = "operator_declined"
        return out
    t_rto_start = rec["ts"]

    # 3. scale_gpu_pool: gọi failover() MỘT LẦN DUY NHẤT
    r = fo.failover(target, backend, wait=60)
    step(3, "scale_gpu_pool", ok=r.get("ok"), steps_done=r.get("steps"), error=r.get("error"))

    # 4. verify_state_replica: chỉ đọc kết quả từ bước 3
    step(4, "verify_state_replica", rpo_seconds=r.get("rpo_seconds"),
         docs_lost=r.get("docs_lost"), embed_model_version=r.get("embed_model_version"))

    # 5. dns_cutover: đọc lại kết quả
    cutover = "5_dns_cutover" in r.get("steps", [])
    step(5, "dns_cutover", ok=cutover)
    if not r.get("ok") or not cutover:
        out["error"] = r.get("error", "cutover_failed")
        return out

    # 6. golden signals: 10 request thật vào region phụ
    lat, errs = [], 0
    for _ in range(10):
        t0 = time.time()
        try:
            ok = httpx.get(f"{URL[target]}/v1/infer", timeout=5).status_code == 200
        except httpx.HTTPError:
            ok = False
        lat.append((time.time() - t0) * 1000)
        errs += 0 if ok else 1
    lat.sort()
    p95 = round(lat[min(len(lat) - 1, int(len(lat) * 0.95))], 1)
    step(6, "verify_golden_signals", requests=10, p95_ms=p95, error_rate=errs / 10)

    # 7. post incident
    elapsed = round(time.time() - t_rto_start, 2)
    step(7, "post_incident", elapsed_s=elapsed,
         next="python tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300")
    out.update(ok=errs == 0, elapsed_s=elapsed, rpo_seconds=r.get("rpo_seconds"),
               docs_lost=r.get("docs_lost"), p95_ms=p95, error_rate=errs / 10)
    return out


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2))
