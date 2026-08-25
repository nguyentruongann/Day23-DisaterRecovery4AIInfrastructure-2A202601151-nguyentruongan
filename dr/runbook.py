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
from dr.health_checker import probe  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def step(n, name, **kw):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
           "step": n, "name": name, **kw}
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(json.dumps(rec, ensure_ascii=False), flush=True)
    return rec


def confirm(auto: bool, msg: str) -> bool:
    return True if auto else input(f"{msg} [y/N] ").strip().lower() in {"y", "yes"}


def _latest_outage(primary: str):
    path = pathlib.Path("chaos/chaos-events.jsonl")
    if not path.exists():
        return None
    events = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    matches = [e for e in events if e.get("action") == "kill" and e.get("region") == primary]
    return matches[-1] if matches else None


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    started = time.time()
    probe_results, next_probe = [], time.monotonic()
    for attempt in range(3):
        primary_ready, primary_reason = probe(primary, timeout=2.0)
        try:
            target_alive = httpx.get(f"{URL[target]}/healthz", timeout=2.0).status_code == 200
        except httpx.HTTPError:
            target_alive = False
        probe_results.append({"attempt": attempt + 1, "primary_ready": primary_ready,
                              "primary_reason": primary_reason, "target_alive": target_alive})
        if attempt < 2:
            next_probe += 5.0
            time.sleep(max(0.0, next_probe - time.monotonic()))
    outage_confirmed = all(not item["primary_ready"] for item in probe_results)
    target_alive = all(item["target_alive"] for item in probe_results)
    step(1, "xac_nhan_outage", primary=primary, target=target,
         outage_confirmed=outage_confirmed, target_alive=target_alive, probes=probe_results)
    if not outage_confirmed or not target_alive:
        return {"ok": False, "failed_step": 1, "reason": "outage_not_confirmed_or_target_down"}
    outage = _latest_outage(primary)
    incident = step(2, "thong_bao_incident", primary=primary,
                    outage_ts=outage.get("ts") if outage else None,
                    outage_iso=outage.get("iso") if outage else None,
                    notification_delay_s=(None if not outage else round(time.time()-outage["ts"], 2)))
    if not confirm(auto, f"Region {primary} da fail 3 lan; cutover sang region {target}?"):
        step(7, "post_incident", ok=False, reason="operator_cancelled")
        return {"ok": False, "failed_step": 2, "reason": "operator_cancelled"}
    result = fo.failover(target, backend, wait=60.0)
    step(3, "scale_gpu_pool", failover_called_once=True, ok=result.get("ok", False),
         failed_step=result.get("failed_step"))
    if not result.get("ok"):
        step(7, "post_incident", ok=False, elapsed_s=round(time.time()-started, 2),
             reason=result.get("reason") or result.get("error"))
        return result
    restored_state = result.get("state", {})
    step(4, "verify_state_replica", vector_count=restored_state.get("count"),
         weights=restored_state.get("weights"),
         embed_model_version=result.get("restore", {}).get("embed_model_version"))
    step(5, "dns_cutover", ok=result.get("active_region") == target,
         active_region=result.get("active_region"))
    latencies, errors, served_by = [], 0, []
    for i in range(10):
        t0 = time.perf_counter()
        try:
            response = httpx.get(f"{URL[target]}/v1/infer", params={"q": f"golden-{i}"}, timeout=3)
            body = response.json()
            errors += response.status_code != 200
            served_by.append(body.get("region"))
        except httpx.HTTPError:
            errors += 1
        latencies.append((time.perf_counter()-t0)*1000)
    ordered = sorted(latencies)
    p95 = ordered[min(len(ordered)-1, int(0.95*len(ordered)))]
    step(6, "verify_golden_signals", requests=10, p95_latency_ms=round(p95, 2),
         error_rate=round(errors/10, 3), served_by=served_by)
    summary = step(7, "post_incident", ok=errors == 0, elapsed_s=round(time.time()-started, 2),
                   incident_ts=incident["ts"],
                   measure_command="python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300")
    return {"ok": errors == 0, "target": target, "failover": result,
            "golden_signals": {"p95_latency_ms": round(p95, 2), "error_rate": round(errors/10, 3)},
            "summary": summary}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2))
