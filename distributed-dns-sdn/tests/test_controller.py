"""Offline tests of the SDN controller logic using a fake OpenFlow datapath
(no Mininet / OVS needed). They serialize every OpenFlow message, so invalid
match fields or actions are caught here.

Run:  DNS_SDN_NO_THREADS=1 DNS_SDN_EVENT_LOG=/dev/null python3 -m unittest tests.test_controller -v
"""
import json
import os
import sys
import time
import types
import unittest

os.environ["DNS_SDN_NO_THREADS"] = "1"
os.environ.setdefault("DNS_SDN_EVENT_LOG", os.devnull)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from os_ken.controller import ofp_event                       # noqa: E402
from os_ken.lib.packet import packet, ethernet, ipv4, udp, arp  # noqa: E402
from os_ken.ofproto import ofproto_v1_3, ofproto_v1_3_parser   # noqa: E402

import sdn_controller as sc                                    # noqa: E402

CLIENT_IP, CLIENT_MAC, CLIENT_PORT = "10.0.0.10", "00:00:00:00:00:01", 1
SRV = {"10.0.0.20": ("00:00:00:00:00:02", 2), "10.0.0.30": ("00:00:00:00:00:03", 3),
       "10.0.0.40": ("00:00:00:00:00:04", 4)}


class FakeDP:
    def __init__(self):
        self.id = 1
        self.ofproto = ofproto_v1_3
        self.ofproto_parser = ofproto_v1_3_parser
        self.sent = []

    def send_msg(self, m):
        m.xid = len(self.sent) + 1
        m.serialize()                       # raises if the message is malformed
        self.sent.append(m)

    def flowmods(self):
        return [m for m in self.sent if isinstance(m, ofproto_v1_3_parser.OFPFlowMod)]

    def packetouts(self):
        return [m for m in self.sent if isinstance(m, ofproto_v1_3_parser.OFPPacketOut)]


def udp_frame(src_mac, dst_mac, src_ip, dst_ip, sport, dport, payload=b"{}"):
    p = packet.Packet()
    p.add_protocol(ethernet.ethernet(ethertype=0x0800, dst=dst_mac, src=src_mac))
    p.add_protocol(ipv4.ipv4(src=src_ip, dst=dst_ip, proto=17))
    p.add_protocol(udp.udp(src_port=sport, dst_port=dport))
    p.add_protocol(payload)
    p.serialize()
    return bytes(p.data)


def arp_frame(opcode, src_mac, src_ip, dst_ip, dst_mac="00:00:00:00:00:00"):
    eth_dst = "ff:ff:ff:ff:ff:ff" if opcode == arp.ARP_REQUEST else dst_mac
    p = packet.Packet()
    p.add_protocol(ethernet.ethernet(ethertype=0x0806, dst=eth_dst, src=src_mac))
    p.add_protocol(arp.arp(opcode=opcode, src_mac=src_mac, src_ip=src_ip,
                           dst_mac=dst_mac, dst_ip=dst_ip))
    p.serialize()
    return bytes(p.data)


def action_fields(flowmod):
    out = []
    for inst in flowmod.instructions:
        for a in inst.actions:
            if isinstance(a, ofproto_v1_3_parser.OFPActionSetField):
                out.append((a.key, a.value))
            elif isinstance(a, ofproto_v1_3_parser.OFPActionOutput):
                out.append(("output", a.port))
    return out


class ControllerTests(unittest.TestCase):
    def setUp(self):
        sc.MODE = "sdn"
        self.app = sc.SimpleSwitch13()
        self.dp = FakeDP()
        self.app.datapaths[1] = self.dp
        # learn the client and the three servers (as ARP/IP packet-ins would)
        self.packet_in(CLIENT_PORT, arp_frame(arp.ARP_REQUEST, CLIENT_MAC, CLIENT_IP, "10.0.0.20"))
        for ip, (mac, port) in SRV.items():
            self.packet_in(port, arp_frame(arp.ARP_REQUEST, mac, ip, CLIENT_IP))
        self.dp.sent.clear()

    def packet_in(self, port, data):
        msg = ofproto_v1_3_parser.OFPPacketIn(
            self.dp, buffer_id=ofproto_v1_3.OFP_NO_BUFFER, total_len=len(data),
            reason=ofproto_v1_3.OFPR_NO_MATCH, table_id=0, cookie=0,
            match=ofproto_v1_3_parser.OFPMatch(in_port=port), data=data)
        self.app.packet_in_handler(ofp_event.EventOFPPacketIn(msg))

    def bring_up(self, *ips):
        for s in self.app.servers:
            if s["ip"] in ips:
                self.app._send_probe(s)
                seq = s["outstanding"]["seq"]
                reply = json.dumps({"type": "health_reply", "id": seq, "status": "ok",
                                    "qlen": 0, "qps": 0.0}).encode()
                mac, port = SRV[s["ip"]]
                self.packet_in(port, udp_frame(mac, sc.PROBE_MAC, s["ip"], sc.PROBE_IP,
                                               s["port"], sc.PROBE_SPORT, reply))
        self.dp.sent.clear()

    def query(self, sport=40000, src_ip=CLIENT_IP, keep=False):
        if not keep:
            self.dp.sent.clear()
        self.packet_in(CLIENT_PORT, udp_frame(CLIENT_MAC, sc.VIP_MAC, src_ip, sc.VIP_IP,
                                              sport, sc.VIP_PORT, b'{"type":"query"}'))

    def redirected_to(self):
        fm = [m for m in self.dp.flowmods() if m.priority == 20]
        if not fm:
            return None
        return dict(action_fields(fm[0])).get("ipv4_dst")

    # ---------------------------------------------------------------- tests
    def test_arp_proxy_answers_for_vip(self):
        self.packet_in(CLIENT_PORT, arp_frame(arp.ARP_REQUEST, CLIENT_MAC, CLIENT_IP, sc.VIP_IP))
        out = self.dp.packetouts()
        self.assertEqual(len(out), 1)
        reply = packet.Packet(out[0].data).get_protocol(arp.arp)
        self.assertEqual((reply.opcode, reply.src_mac, reply.src_ip),
                         (arp.ARP_REPLY, sc.VIP_MAC, sc.VIP_IP))
        self.assertEqual(out[0].actions[0].port, CLIENT_PORT)

    def test_probe_reply_marks_server_up_and_probe_is_well_formed(self):
        s = self.app.servers[0]
        self.assertEqual(s["status"], "down")
        self.app._send_probe(s)
        probe = packet.Packet(self.dp.packetouts()[-1].data)
        self.assertEqual(probe.get_protocol(ipv4.ipv4).dst, "10.0.0.20")
        self.assertEqual(probe.get_protocol(udp.udp).dst_port, 5001)
        self.bring_up("10.0.0.20")
        self.assertEqual(s["status"], "up")
        self.assertIsNotNone(s["rtt_ms"])

    def test_vip_query_is_rewritten_to_backend_with_reverse_flow(self):
        self.bring_up("10.0.0.20", "10.0.0.30")
        self.query()
        fm = [m for m in self.dp.flowmods() if m.priority == 20]
        self.assertEqual(len(fm), 2)
        fwd, rev = fm
        self.assertEqual(dict(action_fields(fwd)), {
            "eth_dst": "00:00:00:00:00:02", "ipv4_dst": "10.0.0.20", "udp_dst": 5001, "output": 2})
        self.assertEqual(dict(action_fields(rev)), {
            "eth_src": sc.VIP_MAC, "ipv4_src": sc.VIP_IP, "udp_src": sc.VIP_PORT, "output": 1})
        self.assertEqual(fwd.match["ipv4_dst"], sc.VIP_IP)
        self.assertEqual(rev.match["ipv4_src"], "10.0.0.20")
        self.assertEqual(len(self.dp.packetouts()), 1)      # first packet is forwarded too

    def test_failover_redirects_after_server_goes_down(self):
        self.bring_up("10.0.0.20", "10.0.0.30")
        self.query(40001)
        self.assertEqual(self.redirected_to(), "10.0.0.20")
        self.dp.sent.clear()
        self.app._mark_down(self.app.servers[0], "test")
        deletes = [m for m in self.dp.flowmods()
                   if m.command == ofproto_v1_3.OFPFC_DELETE]
        self.assertTrue(deletes and deletes[0].cookie == sc.COOKIE_SERVER + 1)
        self.query(40002)
        self.assertEqual(self.redirected_to(), "10.0.0.30")

    def test_no_healthy_server_drops_and_counts(self):
        self.query()
        self.assertIsNone(self.redirected_to())
        self.assertEqual(self.app.counters["no_healthy_server"], 1)

    def test_roundrobin_alternates(self):
        self.bring_up("10.0.0.20", "10.0.0.30", "10.0.0.40")
        self.app.policy = "roundrobin"
        seen = []
        for i in range(6):
            self.query(41000 + i)
            seen.append(self.redirected_to())
        self.assertEqual(set(seen), {"10.0.0.20", "10.0.0.30", "10.0.0.40"})
        self.assertEqual(seen[:3], seen[3:])

    def test_leastloaded_prefers_short_queue(self):
        self.bring_up("10.0.0.20", "10.0.0.30", "10.0.0.40")
        self.app.policy = "leastloaded"
        self.app.servers[0]["qlen"] = 50
        self.app.servers[1]["qlen"] = 5
        self.app.servers[2]["qlen"] = 20
        self.query()
        self.assertEqual(self.redirected_to(), "10.0.0.30")

    def test_missed_probes_mark_down_after_threshold(self):
        self.bring_up("10.0.0.20")
        s = self.app.servers[0]
        self.app._probe_tick()                       # sends probe #1 (outstanding)
        for _ in range(sc.MISS_THRESHOLD - 1):
            self.app._probe_tick()
            self.assertEqual(s["status"], "up")
        self.app._probe_tick()
        self.assertEqual(s["status"], "down")

    def test_link_down_marks_server_down_immediately(self):
        self.bring_up("10.0.0.20")
        ofp = ofproto_v1_3
        desc = types.SimpleNamespace(port_no=2, state=ofp.OFPPS_LINK_DOWN)
        msg = types.SimpleNamespace(datapath=self.dp, desc=desc, reason=ofp.OFPPR_MODIFY)
        self.app.port_status_handler(types.SimpleNamespace(msg=msg))
        self.assertEqual(self.app.servers[0]["status"], "down")

    def test_rate_limit_blocks_flooder_with_drop_flow(self):
        self.bring_up("10.0.0.20")
        self.app.rate_limit = 20
        self.dp.sent.clear()
        for i in range(30):
            self.query(42000 + i, src_ip="10.0.0.60", keep=True)
        drops = [m for m in self.dp.flowmods() if m.priority == 30]
        self.assertEqual(len(drops), 1)
        self.assertEqual(drops[0].match["ipv4_src"], "10.0.0.60")
        self.assertEqual(sum(len(i.actions) for i in drops[0].instructions), 0)  # drop
        self.assertEqual(drops[0].hard_timeout, sc.BLOCK_SECONDS)
        self.assertIn("10.0.0.60", self.app.blocked)
        self.assertGreater(self.app.counters["dropped_rate_limited"], 0)

    def test_static_mode_does_not_redirect(self):
        self.bring_up("10.0.0.20")
        self.app.mode = "static"
        self.query()
        self.assertIsNone(self.redirected_to())

    def test_control_commands(self):
        r = self.app.apply_command({"cmd": "set", "policy": "roundrobin", "mode": "sdn",
                                    "rate_limit": 77})
        self.assertTrue(r["ok"])
        self.assertEqual((self.app.policy, self.app.rate_limit), ("roundrobin", 77))
        self.assertFalse(self.app.apply_command({"cmd": "set", "policy": "bogus"})["ok"])
        self.assertTrue(self.app.apply_command({"cmd": "status"})["ok"])


if __name__ == "__main__":
    unittest.main()
