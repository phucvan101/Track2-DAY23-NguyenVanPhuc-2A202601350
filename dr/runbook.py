"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — CHỜ health checker (reports/health-events.jsonl) tự báo
                              UNHEALTHY cho primary, KHÔNG tự probe nhanh hơn nó. Runbook
                              là người PHẢN ỨNG với alert, không phải người phát hiện —
                              nếu tự probe rồi cutover sớm hơn health_checker.py, đo được
                              t_cutover < t_detect và measure_rto.py đánh dấu drill INVALID
                              (§4 Anti-Patterns: bỏ qua detection floor để failover nhanh
                              hơn = tăng rủi ro flapping). Chỉ probe trực tiếp (dùng
                              health_checker.probe, nhiều lần, không tin 1 lần fail) làm
                              fallback nếu health_checker.py không chạy song song.
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
from dr import health_checker as hc  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
HEALTH_LOG = pathlib.Path("reports/health-events.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def step(n, name, **kw):
    """Ghi 1 dòng {ts, iso, step, name, ...} vào LOG."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
           "step": n, "name": name, **kw}
    with LOG.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    print("RUNBOOK", json.dumps(rec))
    return rec


def confirm(auto: bool, msg: str) -> bool:
    if auto:
        return True
    ans = input(f"{msg} [y/N] ").strip().lower()
    return ans in ("y", "yes")


def wait_for_health_detection(primary: str, since: float, timeout: float, poll: float = 1.0):
    """Doi reports/health-events.jsonl ghi UNHEALTHY cho `primary` (event xay ra sau
    `since`), toi da `timeout` giay. Day la cach dam bao t_cutover >= t_detect."""
    deadline = time.time() + timeout
    while True:
        if HEALTH_LOG.exists():
            for line in HEALTH_LOG.read_text().splitlines():
                if not line.strip():
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (e.get("event") == "state_change" and e.get("region") == primary
                        and e.get("to") == "UNHEALTHY" and e.get("ts", 0) >= since):
                    return e
        if time.time() >= deadline:
            return None
        time.sleep(poll)


def confirm_outage_by_probe(primary: str, target: str, rounds: int = 3, interval: float = 2.0):
    """Fallback khi khong co dr/health_checker.py chay song song: tu probe truc tiep,
    van doi nhieu lan lien tiep, dung tin 1 lan fail."""
    checks = {primary: [], target: []}
    for i in range(rounds):
        for r in (primary, target):
            ready, reason = hc.probe(r, 2.0)
            checks[r].append({"ready": ready, "reason": reason})
        if i < rounds - 1:
            time.sleep(interval)
    return checks, sum(1 for c in checks[primary] if not c["ready"]) >= rounds


def run(primary: str, target: str, backend: str, auto: bool, health_wait: float = 90.0) -> dict:
    # 1. xac_nhan_outage: cho alert cua health_checker.py; fallback tu probe neu no im lang.
    t_start = time.time()
    detection = wait_for_health_detection(primary, since=t_start - 3.0, timeout=health_wait)
    probe_checks = None
    if detection is not None:
        outage_confirmed = True
    else:
        probe_checks, outage_confirmed = confirm_outage_by_probe(primary, target)
    step(1, "xac_nhan_outage", primary=primary, target=target,
         detected_by_health_checker=detection, fallback_probe_checks=probe_checks,
         outage_confirmed=outage_confirmed)

    if not outage_confirmed:
        step(2, "thong_bao_incident", skipped=True,
             reason="outage khong duoc xac nhan: health_checker.py khong bao UNHEALTHY "
                    "trong health_wait giay, va probe truc tiep cung khong du 3 lan fail")
        return {"ok": False, "reason": "outage_not_confirmed"}

    if not confirm(auto, f"Xac nhan outage region-{primary}. Failover sang region-{target}?"):
        step(2, "thong_bao_incident", aborted=True, reason="operator tu choi confirm")
        return {"ok": False, "reason": "operator_declined"}

    t_incident_start = time.time()
    step(2, "thong_bao_incident", primary=primary, target=target,
         t_operator_notified=t_incident_start)

    # 3. scale_gpu_pool: goi failover.failover MOT LAN DUY NHAT.
    result = fo.failover(target, backend, wait=60.0)
    step(3, "scale_gpu_pool", target=target, ok=result.get("ok"), reason=result.get("reason"))

    # 4. verify_state_replica: chi doc lai ket qua cua buoc 3, khong goi lai failover.
    step(4, "verify_state_replica", target=target, rpo=result.get("rpo"),
         embed_model_version=result.get("embed_model_version"),
         state_before=result.get("state_before"))

    # 5. dns_cutover: chi doc lai ket qua cutover.
    step(5, "dns_cutover", target=target, ok=result.get("ok"), waited_s=result.get("waited_s"))

    if not result.get("ok"):
        step(7, "post_incident", ok=False, reason=result.get("reason"),
             elapsed_s=round(time.time() - t_incident_start, 2))
        return {"ok": False, "failover": result}

    # 6. verify_golden_signals: 10 request that vao region phu.
    latencies = []
    errors = 0
    for _ in range(10):
        t0 = time.time()
        try:
            r = httpx.get(f"{URL[target]}/v1/infer", timeout=3.0)
            latencies.append((time.time() - t0) * 1000)
            if r.status_code != 200:
                errors += 1
        except Exception:
            latencies.append((time.time() - t0) * 1000)
            errors += 1
    latencies.sort()
    p95 = latencies[max(0, int(len(latencies) * 0.95) - 1)] if latencies else None
    error_rate = errors / max(1, len(latencies))
    step(6, "verify_golden_signals", p95_ms=round(p95, 1) if p95 is not None else None,
         error_rate=round(error_rate, 3), sample_size=len(latencies))

    # 7. post_incident
    elapsed_s = round(time.time() - t_incident_start, 2)
    step(7, "post_incident", ok=True, elapsed_s=elapsed_s,
         measure_cmd="python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl "
                      "--target-rto 300")

    return {"ok": True, "failover": result, "elapsed_s": elapsed_s}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    p.add_argument("--health-wait", type=float, default=90.0,
                    help="toi da bao nhieu giay cho health_checker.py bao UNHEALTHY "
                         "truoc khi fallback ve tu probe truc tiep")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto, a.health_wait), indent=2))
