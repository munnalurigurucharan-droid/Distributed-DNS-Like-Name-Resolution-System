"""Local (no Mininet) tests of the UDP application: resolution, cache, timeout,
failover, replication, health reply. Run:  python3 -m unittest tests.test_dns_udp -v
"""
import os
import socket
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import dns_common as dc          # noqa: E402
from dns_server import DnsServer  # noqa: E402
from client import DnsClient      # noqa: E402

ZONE = dc.load_zone()
LOOP = "127.0.0.1"


def free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind((LOOP, 0))
    p = s.getsockname()[1]
    s.close()
    return p


def make_server(name, **kw):
    port = free_port()
    srv = DnsServer(name, LOOP, port, ZONE, verbose=False, **kw)
    srv.start_background()
    return srv


class UdpAppTests(unittest.TestCase):
    def setUp(self):
        self.servers = []

    def tearDown(self):
        for s in self.servers:
            s.stop()

    def srv(self, name, **kw):
        s = make_server(name, **kw)
        self.servers.append(s)
        return s

    def backends(self, *servers):
        return [(LOOP, s.port, s.name) for s in servers]

    def test_resolution_and_not_found(self):
        a = self.srv("A")
        c = DnsClient(mode="direct", backends=self.backends(a), timeout=0.5)
        r = c.resolve("EXAMPLE.com.")
        self.assertEqual((r.status, r.ip, r.server), ("success", "10.0.0.10", "A"))
        self.assertEqual(c.resolve("nope.local").status, "not_found")

    def test_cache_hit_is_faster_and_honours_ttl(self):
        a = self.srv("A")
        c = DnsClient(mode="direct", backends=self.backends(a), timeout=0.5)
        first = c.resolve("example.com")
        second = c.resolve("example.com")
        self.assertFalse(first.cache_hit)
        self.assertTrue(second.cache_hit)
        self.assertEqual(c.stats["cache_hits"], 1)
        # force expiry
        st, ip, srvname, _ = c.cache["example.com"]
        c.cache["example.com"] = (st, ip, srvname, time.monotonic() - 1)
        self.assertFalse(c.resolve("example.com").cache_hit)

    def test_timeout_and_failover_direct_mode(self):
        a, b = self.srv("A"), self.srv("B")
        backends = self.backends(a, b)
        a.stop()
        c = DnsClient(mode="direct", backends=backends, timeout=0.3, cache=False)
        t0 = time.time()
        r = c.resolve("example.com")
        self.assertEqual((r.status, r.server), ("success", "B"))
        self.assertEqual(r.timeouts, 1)
        self.assertGreaterEqual(time.time() - t0, 0.29)

    def test_all_servers_down_reports_timeout(self):
        a = self.srv("A")
        backends = self.backends(a)
        a.stop()
        c = DnsClient(mode="direct", backends=backends, timeout=0.2, cache=False)
        self.assertEqual(c.resolve("example.com").status, "timeout")

    def test_stray_reply_with_wrong_txid_is_ignored(self):
        # a fake server that answers with a wrong id first, then the right one
        port = free_port()
        fake = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        fake.bind((LOOP, port))
        import threading

        def run():
            data, addr = fake.recvfrom(2048)
            q = dc.decode(data)
            fake.sendto(dc.encode({"type": "response", "id": q["id"] + 1, "status": "success",
                                   "ip": "6.6.6.6", "ttl": 5, "server": "evil"}), addr)
            fake.sendto(dc.encode({"type": "response", "id": q["id"], "status": "success",
                                   "ip": "1.2.3.4", "ttl": 5, "server": "real"}), addr)
        threading.Thread(target=run, daemon=True).start()
        c = DnsClient(mode="direct", backends=[(LOOP, port, "fake")], timeout=1, cache=False)
        r = c.resolve("example.com")
        fake.close()
        self.assertEqual((r.ip, r.server), ("1.2.3.4", "real"))

    def test_register_replicates_to_peer(self):
        b = self.srv("B")
        a = self.srv("A", peers=[(LOOP, b.port)])
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1)
        s.sendto(dc.encode({"type": "register", "id": 7, "domain": "new.local",
                            "ip": "9.9.9.9", "ttl": 30}), (LOOP, a.port))
        self.assertEqual(dc.decode(s.recvfrom(2048)[0])["status"], "ok")
        s.close()
        time.sleep(0.2)
        c = DnsClient(mode="direct", backends=self.backends(b), timeout=0.5, cache=False)
        self.assertEqual(c.resolve("new.local").ip, "9.9.9.9")

    def test_health_reply_is_instant_even_when_server_is_busy(self):
        a = self.srv("A", service_ms=200, queue_max=50)
        q = dc.encode({"type": "query", "id": 1, "domain": "example.com"})
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for _ in range(10):
            s.sendto(q, (LOOP, a.port))
        time.sleep(0.1)
        s.settimeout(0.3)
        s.sendto(dc.encode({"type": "health", "id": 99}), (LOOP, a.port))
        reply = None
        while reply is None or reply.get("type") != "health_reply":
            reply = dc.decode(s.recvfrom(2048)[0])
        s.close()
        self.assertEqual(reply["status"], "ok")
        self.assertGreater(reply["qlen"], 0)   # backlog visible to the controller

    def test_overload_drops_when_queue_full(self):
        a = self.srv("A", service_ms=100, queue_max=3)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        q = dc.encode({"type": "query", "id": 1, "domain": "example.com"})
        for _ in range(30):
            s.sendto(q, (LOOP, a.port))
        time.sleep(0.2)
        s.close()
        self.assertGreater(a.dropped, 10)


if __name__ == "__main__":
    unittest.main()
