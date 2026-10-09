"""BƯỚC 3b — SINH VIÊN VIẾT. Cutover sang region phụ.

5 bước, THỨ TỰ QUAN TRỌNG (§2 Kiến Trúc Tham Chiếu: DNS/LB, compute, state là 3 lớp riêng):
  1_verify_target    — /v1/state của region phụ: weights? vector count? pool_state?
  2_restore_snapshot — gọi state/snapshot.py get + state/snapshot.py rpo()
                       Log BẮT BUỘC: rpo_seconds, docs_lost, embed_model_version.
                       (§3: "backup index nhưng quên backup embedding model version
                        -> index không tương thích khi restore")
  3_scale_pool       — ghi "full" vào state/region-<t>/pool_state (warm -> full)
  4_wait_ready       — POLL /readyz tới khi 200. Region phụ có WARMUP_SECONDS —
                       đây là GPU pool warm-up của §4, nó nằm trong RTO của bạn.
  5_dns_cutover      — ghi region đích vào edge/active_region

BẪY: nếu bạn đổi edge/active_region TRƯỚC bước 4, user sẽ nhận 503 từ CẢ HAI region
và RTO của bạn dài hơn, không ngắn hơn. Nếu bước 4 timeout -> ABORT, KHÔNG cutover.

Mỗi bước ghi 1 dòng vào reports/failover-events.jsonl với ts + step.
Không có dòng 5_dns_cutover = tools/measure_rto.py không tìm được t_cutover = mất điểm.

Chạy:  python dr/failover.py --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from state import snapshot  # noqa: E402

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
LOG = pathlib.Path("reports/failover-events.jsonl")


def emit(**kw):
    """Append 1 dòng JSONL có ts + iso vào LOG, và print ra stdout."""
    now = time.time()
    rec = {"ts": now, "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)), **kw}
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    print(json.dumps(rec), flush=True)


def state_of(region: str) -> dict:
    return httpx.get(f"{URL[region]}/v1/state", timeout=3).json()


def failover(target: str, backend: str, wait: float) -> dict:
    """5 bước failover, đúng thứ tự. Bước 4 timeout -> ABORT, KHÔNG cutover."""
    primary = "a" if target == "b" else "b"
    result = {"ok": False, "target": target, "steps": []}

    # 1. verify_target
    try:
        st = state_of(target)
    except httpx.HTTPError as e:
        emit(step="1_verify_target", ok=False, error=type(e).__name__)
        result["error"] = "target_unreachable"
        return result
    emit(step="1_verify_target", ok=True, **{k: st.get(k) for k in ("pool_state", "weights", "count")})
    result["steps"].append("1_verify_target")

    # 2. restore_snapshot
    meta = snapshot.get(target, backend)
    r = snapshot.rpo(pathlib.Path(f"state/region-{primary}/vectors.sqlite"),
                     pathlib.Path(f"state/region-{target}/vectors.sqlite"))
    result.update(rpo_seconds=r["rpo_seconds"], docs_lost=r["docs_lost"],
                  embed_model_version=meta.get("embed_model_version"))
    emit(step="2_restore_snapshot", ok=True, rpo_seconds=r["rpo_seconds"], docs_lost=r["docs_lost"],
         embed_model_version=meta.get("embed_model_version"))
    result["steps"].append("2_restore_snapshot")

    # 3. scale_pool
    pool = pathlib.Path(f"state/region-{target}/pool_state")
    pool.write_text("full")
    emit(step="3_scale_pool", ok=True, pool_state="full")
    result["steps"].append("3_scale_pool")

    # 4. wait_ready — warm-up GPU pool nằm trong RTO
    t0 = time.time()
    ready = False
    while time.time() - t0 < wait:
        try:
            if httpx.get(f"{URL[target]}/readyz", timeout=2).status_code == 200:
                ready = True
                break
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    emit(step="4_wait_ready", ok=ready, waited_s=round(time.time() - t0, 2))
    if not ready:
        emit(step="abort", ok=False, reason="target_not_ready_before_timeout")
        result["error"] = "target_not_ready"
        return result
    result["steps"].append("4_wait_ready")

    # 5. dns_cutover — chỉ khi target đã ready
    pathlib.Path("edge/active_region").write_text(target)
    emit(step="5_dns_cutover", ok=True, active_region=target)
    result["steps"].append("5_dns_cutover")
    result["ok"] = True
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--wait", type=float, default=60)
    a = p.parse_args()
    print(json.dumps(failover(a.target, a.backend, a.wait), indent=2))
