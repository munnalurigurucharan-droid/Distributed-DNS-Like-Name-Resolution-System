#!/usr/bin/env python3
"""Web dashboard for the DNS + SDN project (standard library only).

Run it on the SAME machine as Mininet and the controller, as root (it enters the Mininet
host namespaces with `mnexec`, exactly like typing `h1 python3 client.py ...` in the CLI):

    sudo python3 app/webui.py            # then open http://127.0.0.1:8080

Order: 1) python3 run_controller.py   2) sudo python3 topology.py (leave the prompt open)
       3) sudo python3 app/webui.py   4) servers can be started from the page.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "experiments"))
import ctl                      # noqa: E402
import dns_common as dc         # noqa: E402

CLIENT = os.path.join(HERE, "client.py")
SERVER = os.path.join(HERE, "dns_server.py")
FLOOD = os.path.join(HERE, "flood.py")
EVENT_LOG = os.environ.get("DNS_SDN_EVENT_LOG", "/tmp/dns_sdn_events.jsonl")
SERVERS = {"Server A": ("h2", 5001), "Server B": ("h3", 5002), "Server C": ("h4", 5003)}
CLIENT_HOSTS = ("h1", "h5")
NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,253}$")
DEVNULL = subprocess.DEVNULL
lock = threading.Lock()
history = []                    # last lookups shown in the page


def host_pid(host):
    out = subprocess.run(["pgrep", "-f", f"mininet:{host}$"], capture_output=True, text=True).stdout.split()
    return out[0] if out else None


def in_host(host, cmd, timeout=10, background=False):
    pid = host_pid(host)
    if not pid:
        raise RuntimeError(f"Mininet host {host} not found. Is `sudo python3 topology.py` running?")
    full = ["mnexec", "-a", pid] + cmd
    if background:
        subprocess.Popen(full, stdout=DEVNULL, stderr=DEVNULL, start_new_session=True)
        return ""
    r = subprocess.run(full, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0 and not r.stdout:
        raise RuntimeError((r.stderr or "command failed").strip()[-300:])
    return r.stdout


def cache_file(host, mode):
    return f"/tmp/dnsui_cache_{host}_{mode}.json"


def tail_events(n=14):
    try:
        with open(EVENT_LOG) as fh:
            lines = fh.readlines()[-n:]
        return [json.loads(x) for x in lines]
    except (OSError, ValueError):
        return []


# ------------------------------------------------------------------- API
def api_status(_):
    try:
        controller = ctl.send({"cmd": "status"}, timeout=1.0)["status"]
    except (OSError, ValueError, KeyError):
        controller = None
    return {"controller": controller, "mininet": bool(host_pid("h1")), "events": tail_events(),
            "history": history[-12:], "servers": {k: v[0] for k, v in SERVERS.items()}}


def api_names(_):
    return {"names": sorted(dc.load_zone())}


def api_query(b):
    domain, host, mode = str(b.get("domain", "")).strip(), b.get("host", "h1"), b.get("mode", "sdn")
    if not NAME_RE.match(domain):
        raise ValueError("Enter a valid domain name")
    if host not in CLIENT_HOSTS or mode not in ("sdn", "direct"):
        raise ValueError("bad host/mode")
    cmd = ["python3", CLIENT, "--json", "--mode", mode, "--query", domain]
    cmd += ["--cache-file", cache_file(host, mode)] if b.get("cache") else ["--no-cache"]
    res = json.loads(in_host(host, cmd))
    res.update(host=host, mode=mode, t=time.strftime("%H:%M:%S"))
    with lock:
        history.append(res)
        del history[:-50]
    return res


def api_burst(b):
    domain, host, mode = str(b.get("domain", "example.com")).strip(), b.get("host", "h1"), b.get("mode", "sdn")
    n = max(1, min(int(b.get("n", 20)), 100))
    if not NAME_RE.match(domain) or host not in CLIENT_HOSTS or mode not in ("sdn", "direct"):
        raise ValueError("bad request")
    rows = json.loads(in_host(host, ["python3", CLIENT, "--json", "--mode", mode, "--query", domain,
                                     "--burst", str(n)], timeout=10 + n))
    by_server, lat = {}, []
    for r in rows:
        by_server[r["server"] or r["status"]] = by_server.get(r["server"] or r["status"], 0) + 1
        lat.append(r["latency_ms"])
    return {"n": n, "by_server": by_server, "mean_ms": sum(lat) / len(lat), "max_ms": max(lat),
            "failed": sum(1 for r in rows if r["status"] not in ("success", "not_found"))}


def api_policy(b):
    pol = b.get("policy")
    return ctl.send({"cmd": "set", "mode": "sdn", "policy": pol})


def api_server(b):
    name, action = b.get("name"), b.get("action")
    if name not in SERVERS or action not in ("start", "stop"):
        raise ValueError("bad request")
    host, port = SERVERS[name]
    pattern = f"dns_server.py --name {name} --port"
    if action == "stop":
        in_host(host, ["pkill", "-f", pattern])
        return {"ok": True, "message": f"{name} stopped"}
    if subprocess.run(["pgrep", "-f", pattern], capture_output=True).returncode == 0:
        return {"ok": True, "message": f"{name} is already running"}
    log = f"/tmp/dns_server_{host}.log"
    pid = host_pid(host)
    if not pid:
        raise RuntimeError(f"Mininet host {host} not found. Is `sudo python3 topology.py` running?")
    with open(log, "w") as fh:
        subprocess.Popen(["mnexec", "-a", pid, "python3", SERVER, "--name", name, "--port", str(port)],
                         stdout=fh, stderr=subprocess.STDOUT, start_new_session=True)
    time.sleep(0.8)
    if subprocess.run(["pgrep", "-f", pattern], capture_output=True).returncode != 0:
        with open(log) as fh:
            raise RuntimeError(f"{name} failed to start: {fh.read().strip()[-250:] or 'no output'}")
    return {"ok": True, "message": f"{name} started (waiting for the controller to see it, ~2 s)"}


def api_link(b):
    name, action = b.get("name"), b.get("action")
    if name not in SERVERS or action not in ("up", "down"):
        raise ValueError("bad request")
    host = SERVERS[name][0]
    port = host[1:]                                   # h2 -> s1-eth2 (links are added in host order)
    # Both ends of the veth cable, like Mininet's `link s1 h2 up|down`.
    r = subprocess.run(["ip", "link", "set", f"s1-eth{port}", action], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"cannot change switch port s1-eth{port}: {r.stderr.strip()}")
    in_host(host, ["ip", "link", "set", f"{host}-eth0", action])
    return {"ok": True, "message": f"{name} link {action}"}


def api_flood(b):
    secs = max(1, min(int(b.get("seconds", 10)), 30))
    in_host("h6", ["python3", FLOOD, "--rate", "800", "--duration", str(secs)], background=True)
    return {"ok": True}


def api_unblock(_):
    return ctl.send({"cmd": "unblock"})


def api_clear_cache(_):
    for h in CLIENT_HOSTS:
        for m in ("sdn", "direct"):
            try:
                os.remove(cache_file(h, m))
            except OSError:
                pass
    return {"ok": True}


GET = {"/api/status": api_status, "/api/names": api_names}
POST = {"/api/query": api_query, "/api/burst": api_burst, "/api/policy": api_policy,
        "/api/server": api_server, "/api/link": api_link, "/api/flood": api_flood,
        "/api/unblock": api_unblock, "/api/clear-cache": api_clear_cache}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            with open(os.path.join(HERE, "ui", "index.html"), "rb") as fh:
                return self._send(200, fh.read(), "text/html; charset=utf-8")
        fn = GET.get(path)
        if not fn:
            return self._send(404, {"error": "not found"})
        self._run(fn, {})

    def do_POST(self):
        fn = POST.get(self.path)
        if not fn:
            return self._send(404, {"error": "not found"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        except ValueError:
            return self._send(400, {"error": "bad json"})
        self._run(fn, body)

    def _run(self, fn, body):
        try:
            out = fn(body)
            if self.command == "POST":
                print(f"[webui] {self.path} {body} -> ok", flush=True)
            self._send(200, out)
        except ValueError as exc:
            print(f"[webui] {self.path} {body} -> BAD REQUEST: {exc}", flush=True)
            self._send(400, {"error": str(exc)})
        except Exception as exc:           # report to the page instead of crashing
            print(f"[webui] {self.path} {body} -> ERROR: {exc}", flush=True)
            self._send(500, {"error": str(exc)})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    a = ap.parse_args()
    if os.geteuid() != 0:
        sys.exit("Run with sudo: the dashboard enters Mininet host namespaces.")
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print(f"Dashboard: http://{a.host}:{a.port}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
