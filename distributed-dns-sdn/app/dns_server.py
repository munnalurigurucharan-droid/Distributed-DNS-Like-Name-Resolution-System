"""DNS-like UDP resolution server.

One program replaces the old dns_server.py / dns_server_b.py. Run one instance per host:

    python3 dns_server.py --name "Server A" --port 5001
    python3 dns_server.py --name "Server B" --port 5002 --service-ms 10 --peers 10.0.0.20:5001

Design
  * A reader thread receives every datagram immediately. Health probes (from the SDN
    controller) are answered right away, so a busy server is not mistaken for a dead one.
  * Queries go into a bounded queue served by a worker thread. --service-ms emulates
    per-query processing time, so the server has a real capacity limit (1000/service_ms qps).
    A full queue drops the query (overload), which the client sees as a timeout.
  * Mappings can be changed at run time with `register` / `delete` messages and are
    replicated best-effort to --peers (this is what makes the system "distributed").
"""
import argparse
import collections
import queue
import random
import socket
import threading
import time

import dns_common as dc


class RateMeter:
    """Events per second over a sliding window."""

    def __init__(self, window=1.0):
        self.window = window
        self.stamps = collections.deque()
        self.lock = threading.Lock()

    def add(self):
        with self.lock:
            self.stamps.append(time.monotonic())

    def rate(self):
        now = time.monotonic()
        with self.lock:
            while self.stamps and now - self.stamps[0] > self.window:
                self.stamps.popleft()
            return len(self.stamps) / self.window


class DnsServer:
    def __init__(self, name, host, port, zone, service_ms=0.0, queue_max=256,
                 drop_rate=0.0, peers=(), verbose=True):
        self.name = name
        self.host = host
        self.port = port
        self.zone = dict(zone)
        self.zone_lock = threading.Lock()
        self.service_s = service_ms / 1000.0
        self.drop_rate = drop_rate
        self.peers = list(peers)
        self.verbose = verbose
        self.queue = queue.Queue(maxsize=queue_max)
        self.served = 0
        self.dropped = 0
        self.meter = RateMeter()
        self.stop_event = threading.Event()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, port))
        self.sock.settimeout(0.5)
        self.threads = []

    # ------------------------------------------------------------------ helpers
    def log(self, msg):
        if self.verbose:
            print(f"[{self.name}] {msg}", flush=True)

    def send(self, obj, addr):
        try:
            self.sock.sendto(dc.encode(obj), addr)
        except OSError as exc:
            self.log(f"send error to {addr}: {exc}")

    def resolve(self, domain):
        with self.zone_lock:
            return self.zone.get(dc.normalize(domain))

    # ---------------------------------------------------------------- handlers
    def handle_query(self, msg, addr):
        domain = dc.normalize(msg.get("domain", ""))
        rec = self.resolve(domain)
        if rec:
            resp = {"type": "response", "id": msg.get("id"), "domain": domain,
                    "status": "success", "ip": rec[0], "ttl": rec[1], "server": self.name}
        else:
            resp = {"type": "response", "id": msg.get("id"), "domain": domain,
                    "status": "not_found", "ip": None, "ttl": 10, "server": self.name}
        self.send(resp, addr)
        self.served += 1
        self.meter.add()
        self.log(f"{addr[0]}:{addr[1]} {domain} -> {resp['status']} {resp['ip']}")

    def handle_health(self, msg, addr):
        self.send({"type": "health_reply", "id": msg.get("id"), "status": "ok",
                   "server": self.name, "qlen": self.queue.qsize(),
                   "served": self.served, "dropped": self.dropped,
                   "qps": round(self.meter.rate(), 1)}, addr)

    def handle_admin(self, msg, addr):
        domain = dc.normalize(msg.get("domain", ""))
        if not domain:
            self.send({"type": "ack", "id": msg.get("id"), "status": "invalid_query"}, addr)
            return
        with self.zone_lock:
            if msg["type"] == "register":
                self.zone[domain] = (msg.get("ip"), int(msg.get("ttl", 60)))
            else:
                self.zone.pop(domain, None)
        self.log(f"{msg['type']} {domain} (replicated={bool(msg.get('replicated'))})")
        self.send({"type": "ack", "id": msg.get("id"), "status": "ok", "server": self.name}, addr)
        if not msg.get("replicated"):
            fwd = dict(msg, replicated=True)
            for peer in self.peers:
                self.send(fwd, peer)

    # ------------------------------------------------------------------ threads
    def reader_loop(self):
        while not self.stop_event.is_set():
            try:
                data, addr = self.sock.recvfrom(dc.MAX_DGRAM)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                msg = dc.decode(data)
            except (ValueError, UnicodeDecodeError):
                self.send({"type": "response", "id": None, "domain": None,
                           "status": "invalid_query", "ip": None, "server": self.name}, addr)
                continue
            mtype = msg.get("type", "query")
            if mtype == "health":
                self.handle_health(msg, addr)
            elif mtype in ("register", "delete"):
                self.handle_admin(msg, addr)
            elif mtype == "query":
                if self.drop_rate and random.random() < self.drop_rate:
                    self.dropped += 1  # emulated packet loss
                    continue
                try:
                    self.queue.put_nowait((msg, addr))
                except queue.Full:
                    self.dropped += 1  # overload: shed load, client will time out

    def worker_loop(self):
        while not self.stop_event.is_set():
            try:
                msg, addr = self.queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if self.service_s:
                time.sleep(self.service_s)
            self.handle_query(msg, addr)

    def serve_forever(self):
        self.log(f"listening on {self.host}:{self.port} "
                 f"(records={len(self.zone)}, service_ms={self.service_s * 1000:.1f})")
        for target in (self.worker_loop, self.reader_loop):
            t = threading.Thread(target=target, daemon=True)
            t.start()
            self.threads.append(t)
        try:
            while not self.stop_event.is_set():
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def start_background(self):
        for target in (self.worker_loop, self.reader_loop):
            t = threading.Thread(target=target, daemon=True)
            t.start()
            self.threads.append(t)

    def stop(self):
        self.stop_event.set()
        try:
            self.sock.close()
        except OSError:
            pass


def parse_peers(text):
    peers = []
    for item in filter(None, (text or "").split(",")):
        host, port = item.strip().rsplit(":", 1)
        peers.append((host, int(port)))
    return peers


def main():
    ap = argparse.ArgumentParser(description="DNS-like UDP name server")
    ap.add_argument("--name", default="Server A")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=5001)
    ap.add_argument("--zone", default=dc.DEFAULT_ZONE, help="path to zone JSON file")
    ap.add_argument("--service-ms", type=float, default=0.0, help="emulated per-query service time")
    ap.add_argument("--queue-max", type=int, default=256)
    ap.add_argument("--drop-rate", type=float, default=0.0, help="emulated query loss probability")
    ap.add_argument("--peers", default="", help="comma list host:port to replicate register/delete to")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    srv = DnsServer(args.name, args.host, args.port, dc.load_zone(args.zone),
                    service_ms=args.service_ms, queue_max=args.queue_max,
                    drop_rate=args.drop_rate, peers=parse_peers(args.peers),
                    verbose=not args.quiet)
    srv.serve_forever()


if __name__ == "__main__":
    main()
