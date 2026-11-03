#!/usr/bin/env python3
"""Turn results/*.csv (+ .meta.json) into results/summary.md and PNG plots.

    python3 experiments/analyze.py [--results results]
"""
import argparse
import csv
import glob
import json
import os
import statistics
from collections import defaultdict


def pct(vals, p):
    if not vals:
        return float("nan")
    vals = sorted(vals)
    k = (len(vals) - 1) * p / 100.0
    lo, hi = int(k), min(int(k) + 1, len(vals) - 1)
    return vals[lo] + (vals[hi] - vals[lo]) * (k - lo)


def load_group(results_dir, exp, label):
    rows = []
    for path in sorted(glob.glob(os.path.join(results_dir, f"{exp}__{label}*.csv"))):
        with open(path) as fh:
            for r in csv.DictReader(fh):
                r["t_epoch"] = float(r["t_epoch"])
                r["latency_ms"] = float(r["latency_ms"])
                r["timeouts"] = int(r["timeouts"])
                r["cache_hit"] = int(r["cache_hit"])
                rows.append(r)
    rows.sort(key=lambda r: r["t_epoch"])
    return rows


def load_meta(results_dir, exp, label):
    path = os.path.join(results_dir, f"{exp}__{label}.meta.json")
    if os.path.exists(path):
        with open(path) as fh:
            return json.load(fh)
    return {}


def summarize(rows):
    ok = [r for r in rows if r["status"] == "success"]
    lat = [r["latency_ms"] for r in ok]
    net = [r["latency_ms"] for r in ok if not r["cache_hit"]]
    servers = defaultdict(int)
    for r in ok:
        if r["server"]:
            servers[r["server"]] += 1
    return {
        "n": len(rows),
        "success_pct": 100.0 * len(ok) / len(rows) if rows else float("nan"),
        "timeouts": sum(r["timeouts"] for r in rows),
        "mean": statistics.fmean(lat) if lat else float("nan"),
        "p50": pct(lat, 50), "p95": pct(lat, 95), "p99": pct(lat, 99),
        "max": max(lat) if lat else float("nan"),
        "net_mean": statistics.fmean(net) if net else float("nan"),
        "cache_hit_pct": 100.0 * sum(r["cache_hit"] for r in rows) / len(rows) if rows else float("nan"),
        "servers": dict(servers),
    }


def failure_metrics(rows, meta):
    """Disruption metrics around the injected failure."""
    tf, tr = meta.get("t_fail"), meta.get("t_restore")
    if tf is None:
        return {}
    window = [r for r in rows if tf <= r["t_epoch"] < tr]
    affected = [r for r in window if r["timeouts"] > 0 or r["status"] != "success"]
    failed = [r for r in window if r["status"] != "success"]
    # recovery = first issue time after tf such that the next 10 queries have no timeouts
    after = [r for r in rows if r["t_epoch"] >= tf]
    recover = None
    for i in range(len(after) - 10):
        if all(a["timeouts"] == 0 and a["status"] == "success" for a in after[i:i + 10]):
            recover = after[i]["t_epoch"] - tf
            break
    detect = None
    for ev in meta.get("controller_events", []):
        if ev["event"] == "server_down" and ev["ts"] >= tf:
            detect = ev["ts"] - tf
            break
    return {"window_queries": len(window), "affected": len(affected), "failed": len(failed),
            "recover_s": recover, "detect_s": detect}


def fmt(x, d=1):
    return "n/a" if x is None or x != x else f"{x:.{d}f}"


EXPERIMENTS = [
    ("e1", "E1 – Lookup latency, no faults (20 qps, cache off)"),
    ("e1c", "E1c – Client cache effect (Zipf s=1.0)"),
    ("e2", "E2 – Server A crash (t=10 s) and restart (t=20 s)"),
    ("e2l", "E2l – Link to Server A down (t=10 s) and up (t=20 s)"),
    ("e3", "E3 – Overload: 140 qps offered, 100 qps capacity per server"),
    ("e4", "E4 – DNS flood from h6 (800 qps for 10 s)"),
    ("e5", "E5 – 10 % query loss on Server A"),
]


def labels_for(results_dir, exp):
    found = set()
    for path in glob.glob(os.path.join(results_dir, f"{exp}__*.csv")):
        base = os.path.basename(path)[len(exp) + 2:-4]
        found.add(base.rsplit("_h", 1)[0] if "_h" in base else base)
    return sorted(found)


def make_plots(results_dir, data):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not installed; skipping plots")
        return

    for exp, title in EXPERIMENTS:
        groups = data.get(exp)
        if not groups:
            continue
        # latency CDF
        fig, ax = plt.subplots(figsize=(6, 4))
        for label, (rows, meta) in groups.items():
            lat = sorted(r["latency_ms"] for r in rows if r["status"] == "success")
            if lat:
                ax.plot(lat, [100.0 * (i + 1) / len(lat) for i in range(len(lat))], label=label)
        ax.set_xscale("log")
        ax.set_xlabel("lookup latency (ms, log scale)")
        ax.set_ylabel("% of successful queries ≤ x")
        ax.set_title(title, fontsize=9)
        ax.grid(True, alpha=0.3)
        ax.legend(fontsize=8)
        fig.tight_layout()
        fig.savefig(os.path.join(results_dir, f"{exp}_latency_cdf.png"), dpi=140)
        plt.close(fig)

        # timeline for experiments with an event
        if exp in ("e2", "e2l", "e4"):
            fig, axes = plt.subplots(len(groups), 1, figsize=(7, 2.6 * len(groups)), sharex=True)
            axes = axes if len(groups) > 1 else [axes]
            for ax, (label, (rows, meta)) in zip(axes, groups.items()):
                t0 = meta.get("t0", rows[0]["t_epoch"] if rows else 0)
                for r in rows:
                    bad = r["status"] != "success"
                    ax.scatter(r["t_epoch"] - t0, min(r["latency_ms"], 3500),
                               s=8, c="red" if bad else ("orange" if r["timeouts"] else "tab:blue"))
                for key, colour in (("t_fail", "k"), ("t_restore", "g"), ("t_attack", "k")):
                    if key in meta:
                        ax.axvline(meta[key] - t0, color=colour, ls="--", lw=1)
                ax.set_ylabel("latency (ms)")
                ax.set_title(label, fontsize=9)
                ax.grid(True, alpha=0.3)
            axes[-1].set_xlabel("time since start (s)   blue=ok  orange=needed retry  red=failed")
            fig.tight_layout()
            fig.savefig(os.path.join(results_dir, f"{exp}_timeline.png"), dpi=140)
            plt.close(fig)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default=os.path.join(os.path.dirname(__file__), "..", "results"))
    args = ap.parse_args()
    rd = os.path.abspath(args.results)

    data, lines = {}, ["# Evaluation summary\n"]
    for exp, title in EXPERIMENTS:
        labels = labels_for(rd, exp)
        if not labels:
            continue
        data[exp] = {}
        lines += [f"\n## {title}\n",
                  "| config | queries | success % | timeouts | mean ms | p50 | p95 | p99 | max ms |"
                  " cache hit % | answered by |",
                  "|---|---|---|---|---|---|---|---|---|---|---|"]
        extra = []
        for label in labels:
            rows, meta = load_group(rd, exp, label), load_meta(rd, exp, label)
            if not rows:
                continue
            data[exp][label] = (rows, meta)
            s = summarize(rows)
            srv = ", ".join(f"{k.split()[-1]}:{v}" for k, v in sorted(s["servers"].items()))
            lines.append(f"| {label} | {s['n']} | {fmt(s['success_pct'])} | {s['timeouts']} | "
                         f"{fmt(s['mean'], 2)} | {fmt(s['p50'], 2)} | {fmt(s['p95'], 2)} | "
                         f"{fmt(s['p99'], 2)} | {fmt(s['max'], 1)} | {fmt(s['cache_hit_pct'])} | {srv} |")
            fm = failure_metrics(rows, meta)
            if fm:
                extra.append(f"| {label} | {fm['window_queries']} | {fm['affected']} | {fm['failed']} | "
                             f"{fmt(fm['recover_s'], 2)} | {fmt(fm['detect_s'], 2)} |")
            if exp == "e4":
                ev = [e for e in meta.get("controller_events", []) if e["event"] == "client_blocked"]
                if ev and "t_attack" in meta:
                    extra.append(f"| {label} | flood blocked {ev[0]['ts'] - meta['t_attack']:.2f} s "
                                 f"after it started | | | | |")
        if extra and exp != "e4":
            lines += ["", "| config | queries during outage | affected (retry/timeout) | failed | "
                          "recovery after failure (s) | controller detection (s) |",
                      "|---|---|---|---|---|---|"] + extra
        elif extra:
            lines += [""] + [e.split("|")[1].strip() + ": " + e.split("|")[2].strip()
                             for e in extra]
    out = os.path.join(rd, "summary.md")
    with open(out, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    print("\n".join(lines))
    make_plots(rd, data)
    print(f"\nwrote {out} and PNG plots in {rd}")


if __name__ == "__main__":
    main()
