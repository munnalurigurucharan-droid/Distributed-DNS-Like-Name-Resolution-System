#!/usr/bin/env python3
"""Run the evaluation experiments inside Mininet and write CSV/JSON to results/.

Prerequisite: the SDN controller is already running in another terminal
    cd app && osken-manager sdn_controller.py

Then (needs root for Mininet):
    sudo python3 experiments/run_experiments.py                 # all experiments
    sudo python3 experiments/run_experiments.py --only e2       # one experiment
    sudo python3 experiments/run_experiments.py --quick         # short smoke run

Experiments (each one compares a static baseline against the SDN approach)
  e1  Latency, no faults        : direct/static  vs  sdn-failover  vs  sdn-roundrobin
  e1c Cache effect              : sdn with and without client cache (Zipf workload)
  e2  Server crash              : direct/static  vs  sdn-failover   (Server A killed, then restarted)
  e2l Link failure              : direct/static  vs  sdn-failover   (s1<->h2 link down, then up)
  e3  Server overload           : direct/static  vs  sdn-roundrobin vs sdn-leastloaded
  e4  DNS flood / rate limiting : sdn without rate limit  vs  sdn with rate limit
  e5  Packet loss on Server A   : direct/static  vs  sdn-leastloaded  (10 % emulated query loss)
"""
import argparse
import json
import os
import subprocess
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
APP = os.path.join(ROOT, "app")
RESULTS = os.path.join(ROOT, "results")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "experiments"))

from mininet.log import setLogLevel, info      # noqa: E402
from topology import build_net, start_net                 # noqa: E402
import ctl                                      # noqa: E402

EVENT_LOG = os.environ.get("DNS_SDN_EVENT_LOG", "/tmp/dns_sdn_events.jsonl")
SERVERS = [("h2", "Server A", 5001), ("h3", "Server B", 5002), ("h4", "Server C", 5003)]
DEVNULL = subprocess.DEVNULL


class Lab:
    def __init__(self, net, scale):
        self.net = net
        self.scale = scale
        self.server_procs = {}

    # ---------------------------------------------------------------- servers
    def start_server(self, host, name, port, service_ms=0.0, queue_max=256, drop_rate=0.0):
        cmd = ["python3", os.path.join(APP, "dns_server.py"), "--name", name, "--port", str(port),
               "--service-ms", str(service_ms), "--queue-max", str(queue_max),
               "--drop-rate", str(drop_rate), "--quiet"]
        self.server_procs[name] = self.net.get(host).popen(cmd, stdout=DEVNULL, stderr=DEVNULL)

    def start_servers(self, **kw):
        for host, name, port in SERVERS:
            self.start_server(host, name, port, **(kw.get(name) or kw.get("all") or {}))

    def stop_servers(self):
        for p in self.server_procs.values():
            if p.poll() is None:
                p.kill()
                p.wait()
        self.server_procs.clear()
        time.sleep(0.3)

    def wait_servers_up(self, names=None, timeout=20):
        names = names or [s[1] for s in SERVERS]
        end = time.time() + timeout
        while time.time() < end:
            st = ctl.send({"cmd": "status"})["status"]["servers"]
            if all(s["status"] == "up" for s in st if s["name"] in names):
                time.sleep(1.0)     # settle: stale "up" from a killed server may still be cached
                return True
            time.sleep(0.3)
        raise RuntimeError("controller never saw the servers as UP; is it running and connected "
                           "to s1? (check: python3 experiments/ctl.py status)")

    # ---------------------------------------------------------------- clients
    def loadgen(self, host, label, out, mode, rate, duration, **extra):
        cmd = ["python3", os.path.join(APP, "loadgen.py"), "--mode", mode, "--rate", str(rate),
               "--duration", str(duration), "--label", label, "--out", out]
        for k, v in extra.items():
            flag = "--" + k.replace("_", "-")
            if v is True:
                cmd.append(flag)
            elif v not in (False, None):
                cmd += [flag, str(v)]
        return self.net.get(host).popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def flood(self, host, rate, duration):
        cmd = ["python3", os.path.join(APP, "flood.py"), "--rate", str(rate),
               "--duration", str(duration)]
        return self.net.get(host).popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    @staticmethod
    def wait(procs):
        for p in procs:
            out, _ = p.communicate()
            if out:
                info("   " + out.decode().strip() + "\n")


def controller_events_since(t0):
    out = []
    try:
        with open(EVENT_LOG) as fh:
            for line in fh:
                rec = json.loads(line)
                if rec["ts"] >= t0:
                    out.append(rec)
    except (OSError, ValueError):
        pass
    return out


def save_meta(name, label, meta):
    meta["controller_status"] = ctl.send({"cmd": "status"})["status"]
    with open(os.path.join(RESULTS, f"{name}__{label}.meta.json"), "w") as fh:
        json.dump(meta, fh, indent=2)


def csv_path(name, label, host=""):
    return os.path.join(RESULTS, f"{name}__{label}{('_' + host) if host else ''}.csv")


# ---------------------------------------------------------------------- configs
def configure(mode, policy="failover", rate_limit=200):
    ctl.send({"cmd": "set", "mode": mode, "policy": policy, "rate_limit": rate_limit})
    time.sleep(0.3)


CONFIGS = {   # label -> (controller mode, policy, client mode)
    "direct-static": ("static", "failover", "direct"),
    "sdn-failover": ("sdn", "failover", "sdn"),
    "sdn-roundrobin": ("sdn", "roundrobin", "sdn"),
    "sdn-leastloaded": ("sdn", "leastloaded", "sdn"),
}


# ------------------------------------------------------------------ experiments
def e1(lab, scale):
    info("\n=== E1: lookup latency, no faults ===\n")
    for label in ("direct-static", "sdn-failover", "sdn-roundrobin"):
        cmode, pol, clmode = CONFIGS[label]
        configure(cmode, pol)
        lab.start_servers()
        lab.wait_servers_up()
        t0 = time.time()
        p = lab.loadgen("h1", label, csv_path("e1", label), clmode, rate=20, duration=20 * scale)
        lab.wait([p])
        save_meta("e1", label, {"t0": t0, "config": label})
        lab.stop_servers()


def e1c(lab, scale):
    info("\n=== E1c: effect of the client cache (Zipf workload) ===\n")
    configure("sdn", "failover")
    for label, cache in (("cache-off", False), ("cache-on", True)):
        lab.start_servers()
        lab.wait_servers_up()
        t0 = time.time()
        p = lab.loadgen("h1", label, csv_path("e1c", label), "sdn", rate=20,
                        duration=30 * scale, zipf=1.0, cache=cache)
        lab.wait([p])
        save_meta("e1c", label, {"t0": t0, "config": label})
        lab.stop_servers()


def failure_run(lab, scale, name, kind):
    for label in ("direct-static", "sdn-failover"):
        cmode, pol, clmode = CONFIGS[label]
        info(f"--- {name} / {label}\n")
        configure(cmode, pol)
        lab.start_servers()
        lab.wait_servers_up()
        dur = 30 * scale
        t0 = time.time()
        procs = [lab.loadgen(h, label, csv_path(name, label, h), clmode, rate=20, duration=dur,
                             seed=i + 1) for i, h in enumerate(("h1", "h5"))]
        meta = {"t0": t0, "config": label, "kind": kind}
        time.sleep(max(0, t0 + 10 * scale - time.time()))
        meta["t_fail"] = time.time()
        if kind == "crash":
            lab.server_procs["Server A"].kill()
        else:
            lab.net.configLinkStatus("s1", "h2", "down")
        time.sleep(max(0, t0 + 20 * scale - time.time()))
        meta["t_restore"] = time.time()
        if kind == "crash":
            lab.start_server("h2", "Server A", 5001)
        else:
            lab.net.configLinkStatus("s1", "h2", "up")
        lab.wait(procs)
        meta["controller_events"] = controller_events_since(t0)
        save_meta(name, label, meta)
        lab.stop_servers()
        if kind == "link":
            lab.net.configLinkStatus("s1", "h2", "up")


def e2(lab, scale):
    info("\n=== E2: Server A crash at t=10s, restart at t=20s ===\n")
    failure_run(lab, scale, "e2", "crash")


def e2l(lab, scale):
    info("\n=== E2l: link s1<->Server A down at t=10s, up at t=20s ===\n")
    failure_run(lab, scale, "e2l", "link")


def e3(lab, scale):
    info("\n=== E3: overload (each server: 100 qps capacity; offered load 140 qps) ===\n")
    for label in ("direct-static", "sdn-roundrobin", "sdn-leastloaded"):
        cmode, pol, clmode = CONFIGS[label]
        configure(cmode, pol)
        lab.start_servers(all={"service_ms": 10, "queue_max": 100})
        lab.wait_servers_up()
        t0 = time.time()
        procs = [lab.loadgen(h, label, csv_path("e3", label, h), clmode, rate=70,
                             duration=20 * scale, seed=i + 1) for i, h in enumerate(("h1", "h5"))]
        lab.wait(procs)
        save_meta("e3", label, {"t0": t0, "config": label, "offered_qps": 140})
        lab.stop_servers()


def e4(lab, scale):
    info("\n=== E4: DNS flood from h6 (800 qps, t=5..15s) vs legitimate h1 (20 qps) ===\n")
    for label, limit in (("no-ratelimit", 0), ("ratelimit-200", 200)):
        configure("sdn", "roundrobin", rate_limit=limit)
        lab.start_servers(all={"service_ms": 5, "queue_max": 100})
        lab.wait_servers_up()
        dur = 25 * scale
        t0 = time.time()
        legit = lab.loadgen("h1", label, csv_path("e4", label), "sdn", rate=20, duration=dur)
        time.sleep(5 * scale)
        t_attack = time.time()
        fl = lab.flood("h6", rate=800, duration=10 * scale)
        lab.wait([legit, fl])
        save_meta("e4", label, {"t0": t0, "t_attack": t_attack, "attack_s": 10 * scale,
                                "config": label, "controller_events": controller_events_since(t0)})
        lab.stop_servers()
        ctl.send({"cmd": "unblock"})


def e5(lab, scale):
    info("\n=== E5: 10 percent query loss on Server A ===\n")
    for label in ("direct-static", "sdn-leastloaded"):
        cmode, pol, clmode = CONFIGS[label]
        configure(cmode, pol)
        lab.start_servers(**{"Server A": {"drop_rate": 0.10}, "Server B": {}, "Server C": {}})
        lab.wait_servers_up()
        t0 = time.time()
        p = lab.loadgen("h1", label, csv_path("e5", label), clmode, rate=20, duration=20 * scale)
        lab.wait([p])
        save_meta("e5", label, {"t0": t0, "config": label})
        lab.stop_servers()


EXPERIMENTS = {"e1": e1, "e1c": e1c, "e2": e2, "e2l": e2l, "e3": e3, "e4": e4, "e5": e5}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=",".join(EXPERIMENTS), help="comma list, e.g. e1,e2")
    ap.add_argument("--quick", action="store_true", help="scale all durations by 0.4")
    args = ap.parse_args()
    if os.geteuid() != 0:
        sys.exit("Run with sudo (Mininet needs root).")
    os.makedirs(RESULTS, exist_ok=True)
    try:
        ctl.send({"cmd": "status"})
    except OSError:
        sys.exit("Cannot reach the controller control socket. Start it first:\n"
                 "  cd app && osken-manager sdn_controller.py")

    setLogLevel("info")
    net = build_net()
    start_net(net)
    lab = Lab(net, 0.4 if args.quick else 1.0)
    try:
        time.sleep(2)   # let the switch connect
        net.pingAll(timeout=2)
        for name in args.only.split(","):
            EXPERIMENTS[name.strip()](lab, lab.scale)
    finally:
        lab.stop_servers()
        net.stop()
    info(f"\nDone. Now run: python3 experiments/analyze.py\n")


if __name__ == "__main__":
    main()
