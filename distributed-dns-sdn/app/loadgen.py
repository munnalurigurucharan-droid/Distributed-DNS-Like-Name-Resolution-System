"""Open-loop load generator / measurement client.

Issues queries at a fixed rate for a fixed time and records one CSV row per query.
Used by the experiments (run inside a Mininet host) and also handy by hand:

    python3 loadgen.py --mode sdn --rate 20 --duration 30 --out /tmp/sdn.csv
"""
import argparse
import csv
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import dns_common as dc
from client import DnsClient


def zipf_weights(n, s):
    w = [1.0 / ((i + 1) ** s) for i in range(n)]
    total = sum(w)
    return [x / total for x in w]


def main():
    ap = argparse.ArgumentParser(description="DNS load generator")
    ap.add_argument("--mode", choices=["sdn", "direct"], default="sdn")
    ap.add_argument("--rate", type=float, default=20.0, help="queries per second")
    ap.add_argument("--duration", type=float, default=30.0)
    ap.add_argument("--timeout", type=float, default=1.0)
    ap.add_argument("--workers", type=int, default=64)
    ap.add_argument("--zone", default=dc.DEFAULT_ZONE)
    ap.add_argument("--zipf", type=float, default=0.0,
                    help="Zipf exponent (0 = uniform names; ~1.0 = skewed, cache-friendly)")
    ap.add_argument("--unknown", type=float, default=0.0,
                    help="fraction of queries for names that do not exist")
    ap.add_argument("--cache", action="store_true", help="enable client cache (default off)")
    ap.add_argument("--label", default="run")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    names = sorted(dc.load_zone(args.zone))
    weights = zipf_weights(len(names), args.zipf) if args.zipf > 0 else None

    client = DnsClient(mode=args.mode, timeout=args.timeout, cache=args.cache)
    rows, lock = [], threading.Lock()
    start = time.time()

    def one(domain, t_sched):
        t_issue = time.time()
        r = client.resolve(domain)
        with lock:
            rows.append([f"{t_issue:.4f}", f"{t_issue - start:.3f}", domain, r.status,
                         r.ip or "", r.server or "", f"{r.latency_ms:.3f}",
                         int(r.cache_hit), r.attempts, r.timeouts, args.label, args.mode])

    interval = 1.0 / args.rate
    n_total = int(args.duration * args.rate)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for i in range(n_total):
            t_sched = start + i * interval
            delay = t_sched - time.time()
            if delay > 0:
                time.sleep(delay)
            if rng.random() < args.unknown:
                domain = f"nonexistent{rng.randint(0, 9999)}.local"
            elif weights:
                domain = rng.choices(names, weights)[0]
            else:
                domain = rng.choice(names)
            pool.submit(one, domain, t_sched)

    rows.sort(key=lambda r: float(r[0]))
    with open(args.out, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["t_epoch", "t_rel", "domain", "status", "ip", "server", "latency_ms",
                    "cache_hit", "attempts", "timeouts", "label", "mode"])
        w.writerows(rows)
    ok = sum(1 for r in rows if r[3] == "success")
    print(f"{args.label}: {len(rows)} queries, {ok} success -> {args.out}")


if __name__ == "__main__":
    main()
