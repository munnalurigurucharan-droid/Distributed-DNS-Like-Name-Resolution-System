"""DNS-like UDP client with TTL cache, timeouts, retries and latency measurement.

Two resolution modes (this is the baseline-vs-SDN switch used in the evaluation):

  sdn     Send every query to the virtual IP (VIP). The SDN controller decides which
          healthy backend answers and redirects the packet in the switch.
          Retries go to the same VIP; after a failure is detected the retry lands on a
          different server without the client knowing.

  direct  Static baseline (what the original project did): client talks to the real
          servers in a fixed order A -> B -> C and fails over by itself after each timeout.

Usage:
    python3 client.py                      # interactive, sdn mode
    python3 client.py --mode direct
    python3 client.py --query example.com
"""
import argparse
import collections
import json
import os
import socket
import time
from dataclasses import asdict, dataclass
from typing import Optional

import dns_common as dc


@dataclass
class Result:
    domain: str
    status: str                    # success | not_found | timeout | error
    ip: Optional[str] = None
    server: Optional[str] = None   # which backend answered (from the response)
    latency_ms: float = 0.0
    cache_hit: bool = False
    attempts: int = 0
    timeouts: int = 0


class DnsClient:
    def __init__(self, mode="sdn", timeout=1.0, retries=None, cache=True,
                 cache_max=1024, negative_ttl=10.0, vip=None, backends=None):
        self.mode = mode
        self.timeout = timeout
        self.cache_enabled = cache
        self.cache_max = cache_max
        self.negative_ttl = negative_ttl
        self.vip = vip or (dc.VIP_IP, dc.VIP_PORT)
        self.backends = backends or dc.BACKENDS
        if mode == "sdn":
            n = retries if retries is not None else len(self.backends)
            self.targets = [(self.vip[0], self.vip[1], "VIP")] * n
        elif mode == "direct":
            self.targets = list(self.backends)
        else:
            raise ValueError("mode must be 'sdn' or 'direct'")
        self.cache = collections.OrderedDict()   # domain -> (status, ip, server, expires_at)
        self.stats = {"queries": 0, "cache_hits": 0, "cache_misses": 0,
                      "timeouts": 0, "failures": 0}

    # ------------------------------------------------------------------ cache
    def _cache_get(self, domain):
        entry = self.cache.get(domain)
        if not entry:
            return None
        status, ip, server, expires = entry
        if time.monotonic() >= expires:
            del self.cache[domain]
            return None
        self.cache.move_to_end(domain)
        return entry

    def _cache_put(self, domain, status, ip, server, ttl):
        if ttl <= 0:
            return
        self.cache[domain] = (status, ip, server, time.monotonic() + ttl)
        self.cache.move_to_end(domain)
        while len(self.cache) > self.cache_max:
            self.cache.popitem(last=False)

    # ---------------------------------------------------------------- resolve
    def resolve(self, domain):
        t0 = time.perf_counter()
        domain = dc.normalize(domain)
        self.stats["queries"] += 1

        if self.cache_enabled:
            hit = self._cache_get(domain)
            if hit:
                self.stats["cache_hits"] += 1
                status, ip, server, _ = hit
                return Result(domain, status, ip, server,
                              (time.perf_counter() - t0) * 1000, cache_hit=True)
            self.stats["cache_misses"] += 1

        attempts = timeouts = 0
        for host, port, label in self.targets:
            attempts += 1
            resp = self._ask(host, port, domain)
            if resp is None:
                timeouts += 1
                self.stats["timeouts"] += 1
                continue
            status = resp.get("status")
            if status in ("success", "not_found"):
                ttl = float(resp.get("ttl") or 0)
                if status == "not_found":
                    ttl = self.negative_ttl
                if self.cache_enabled:
                    self._cache_put(domain, status, resp.get("ip"), resp.get("server"), ttl)
                return Result(domain, status, resp.get("ip"), resp.get("server"),
                              (time.perf_counter() - t0) * 1000,
                              attempts=attempts, timeouts=timeouts)
            # invalid_query or unknown status: treat as a failed attempt and try next target

        self.stats["failures"] += 1
        return Result(domain, "timeout" if timeouts == attempts else "error",
                      latency_ms=(time.perf_counter() - t0) * 1000,
                      attempts=attempts, timeouts=timeouts)

    def _ask(self, host, port, domain):
        """One UDP exchange. Returns the response dict or None on timeout/error."""
        txid = dc.new_txid()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)  # new socket => new source port
        try:
            sock.sendto(dc.encode({"type": "query", "id": txid, "domain": domain}), (host, port))
            deadline = time.monotonic() + self.timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                sock.settimeout(remaining)
                try:
                    data, _addr = sock.recvfrom(dc.MAX_DGRAM)
                except socket.timeout:
                    return None
                try:
                    msg = dc.decode(data)
                except (ValueError, UnicodeDecodeError):
                    continue
                # Ignore stray / late datagrams; only accept the reply to *this* query.
                if msg.get("id") == txid and msg.get("type") == "response":
                    return msg
        except OSError:
            return None
        finally:
            sock.close()


def load_cache_file(client, path):
    """Persist the cache between runs of this program (used by the web dashboard)."""
    try:
        with open(path) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return
    now, mono = time.time(), time.monotonic()
    for domain, (status, ip, server, expires) in data.items():
        if expires > now:
            client.cache[domain] = (status, ip, server, mono + (expires - now))


def save_cache_file(client, path):
    now, mono = time.time(), time.monotonic()
    data = {d: [st, ip, srv, now + (exp - mono)]
            for d, (st, ip, srv, exp) in client.cache.items() if exp > mono}
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


def print_result(r):
    if r.status == "success":
        tag = "Cache hit" if r.cache_hit else "Resolution successful"
        print(f"\n{tag}!\nDomain: {r.domain}\nIP Address: {r.ip}")
        if not r.cache_hit:
            print(f"Responded by: {r.server}  (attempts={r.attempts}, timeouts={r.timeouts})")
    elif r.status == "not_found":
        print(f"\nDomain '{r.domain}' not found (answered by {r.server}).")
    else:
        print(f"\nNo valid answer for '{r.domain}' ({r.status}; "
              f"attempts={r.attempts}, timeouts={r.timeouts}).")
    print(f"Lookup latency: {r.latency_ms:.3f} ms")


def main():
    ap = argparse.ArgumentParser(description="DNS-like UDP client")
    ap.add_argument("--mode", choices=["sdn", "direct"], default="sdn")
    ap.add_argument("--timeout", type=float, default=1.0)
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--query", help="resolve one name and exit")
    ap.add_argument("--json", action="store_true", help="print machine-readable JSON (used by the web UI)")
    ap.add_argument("--burst", type=int, default=0, help="with --query: send N queries, cache off")
    ap.add_argument("--cache-file", help="load/save the cache from/to this JSON file")
    args = ap.parse_args()

    client = DnsClient(mode=args.mode, timeout=args.timeout,
                       cache=not args.no_cache and not args.burst)
    if args.cache_file and client.cache_enabled:
        load_cache_file(client, args.cache_file)

    if args.query and args.burst:
        print(json.dumps([asdict(client.resolve(args.query)) for _ in range(args.burst)]))
        return
    if args.query:
        result = client.resolve(args.query)
        if args.cache_file and client.cache_enabled:
            save_cache_file(client, args.cache_file)
        if args.json:
            print(json.dumps(asdict(result)))
        else:
            print_result(result)
        return

    print(f"DNS client ({args.mode} mode). Commands: <domain> | stats | exit")
    while True:
        try:
            line = input("\nEnter a domain name (or 'exit' to quit): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nDNS client closed.")
            break
        if line == "exit":
            print("DNS client closed.")
            break
        if line == "stats":
            print(client.stats)
            continue
        if not line:
            print("Please enter a domain name.")
            continue
        print_result(client.resolve(line))


if __name__ == "__main__":
    main()
