"""DNS-aware SDN controller (OS-Ken, OpenFlow 1.3).

What it does
  1. L2 learning switch (baseline behaviour, also used in MODE=static).
  2. Virtual IP (VIP) for the DNS service. Clients query VIP:5300; the controller
     answers ARP for the VIP and, for every new query, picks a backend and installs
     rewrite flows (dst IP/MAC/port -> backend, and src -> VIP on the reply path).
  3. Active health monitoring: the controller injects DNS "health" probes into the
     switch and watches the replies. N missed probes => server DOWN. A link-down
     PortStatus event marks a server DOWN immediately. DOWN servers get their flows
     flushed so the next query is redirected to a healthy server.
  4. Policies: failover | roundrobin | leastloaded (uses queue length reported by servers).
  5. Rate limiting: a client sending more than RATE_LIMIT queries per RATE_WINDOW seconds
     is blocked with a drop flow for BLOCK_SECONDS.
  6. A small UDP control socket (127.0.0.1:CTL_PORT) used by experiments/ctl.py to read
     status and change MODE/POLICY/RATE_LIMIT at run time.
  7. JSON-lines event log (DNS_SDN_EVENT_LOG) so experiments can measure detection time.

Environment variables (all optional)
  DNS_SDN_MODE=sdn|static   DNS_SDN_POLICY=failover|roundrobin|leastloaded
  DNS_SDN_PROBE_INTERVAL=0.5   DNS_SDN_MISS_THRESHOLD=3
  DNS_SDN_RATE_LIMIT=200   DNS_SDN_RATE_WINDOW=1.0   DNS_SDN_BLOCK_SECONDS=10
  DNS_SDN_EVENT_LOG=/tmp/dns_sdn_events.jsonl   DNS_SDN_CTL_PORT=9099

Keep the constants below in sync with app/dns_common.py and topology.py.
"""
import json
import logging
import os
import socket
import time
from collections import defaultdict, deque

from os_ken.base import app_manager
from os_ken.controller import ofp_event
from os_ken.controller.handler import (
    CONFIG_DISPATCHER, MAIN_DISPATCHER, DEAD_DISPATCHER, set_ev_cls)
from os_ken.lib import hub
from os_ken.lib.packet import packet, ethernet, ipv4, udp, arp, ether_types
from os_ken.ofproto import ofproto_v1_3

# --------------------------------------------------------------------- config
VIP_IP = os.environ.get("DNS_VIP_IP", "10.0.0.100")
VIP_PORT = int(os.environ.get("DNS_VIP_PORT", "5300"))
VIP_MAC = "02:00:00:00:01:00"
PROBE_IP = "10.0.0.254"            # identity the controller uses to probe servers
PROBE_MAC = "02:00:00:00:02:54"
PROBE_SPORT = 5399

BACKENDS = [                       # priority order = order here
    {"name": "Server A", "ip": "10.0.0.20", "port": 5001},
    {"name": "Server B", "ip": "10.0.0.30", "port": 5002},
    {"name": "Server C", "ip": "10.0.0.40", "port": 5003},
]

MODE = os.environ.get("DNS_SDN_MODE", "sdn")
POLICY = os.environ.get("DNS_SDN_POLICY", "failover")
PROBE_INTERVAL = float(os.environ.get("DNS_SDN_PROBE_INTERVAL", "0.5"))
MISS_THRESHOLD = int(os.environ.get("DNS_SDN_MISS_THRESHOLD", "3"))
RATE_LIMIT = int(os.environ.get("DNS_SDN_RATE_LIMIT", "200"))
RATE_WINDOW = float(os.environ.get("DNS_SDN_RATE_WINDOW", "1.0"))
BLOCK_SECONDS = int(os.environ.get("DNS_SDN_BLOCK_SECONDS", "10"))
FLOW_IDLE = 2
EVENT_LOG = os.environ.get("DNS_SDN_EVENT_LOG", "/tmp/dns_sdn_events.jsonl")
CTL_PORT = int(os.environ.get("DNS_SDN_CTL_PORT", "9099"))
NO_THREADS = os.environ.get("DNS_SDN_NO_THREADS") == "1"   # used by unit tests

POLICIES = ("failover", "roundrobin", "leastloaded")
COOKIE_SERVER = 0xD5000000          # + server index (1-based); lets us flush per server
COOKIE_BLOCK = 0xD5B00000
COOKIE_MASK = 0xFFFFFFFF


class SimpleSwitch13(app_manager.OSKenApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.logger.setLevel(logging.INFO)
        self.mode = MODE
        self.policy = POLICY if POLICY in POLICIES else "failover"
        self.rate_limit = RATE_LIMIT

        self.datapaths = {}
        self.mac_to_port = {}                 # dpid -> {mac: port}
        self.hosts = {}                       # ip -> {"mac","dpid","port"}
        self.rr_index = 0
        self.arp_proxy = {VIP_IP: VIP_MAC, PROBE_IP: PROBE_MAC}

        self.servers = []
        for i, b in enumerate(BACKENDS, start=1):
            self.servers.append({
                "idx": i, "name": b["name"], "ip": b["ip"], "port": b["port"],
                "status": "down", "misses": 0, "outstanding": None,
                "qlen": 0, "qps": 0.0, "rtt_ms": None, "assigned": 0, "probe_seq": 0,
            })
        self.by_ip = {s["ip"]: s for s in self.servers}

        self.query_times = defaultdict(deque)   # client ip -> recent query timestamps
        self.blocked = {}                       # client ip -> unblock time
        self.counters = defaultdict(int)

        self.event("controller_started", mode=self.mode, policy=self.policy,
                   probe_interval=PROBE_INTERVAL, miss_threshold=MISS_THRESHOLD,
                   rate_limit=self.rate_limit)
        if not NO_THREADS:
            hub.spawn(self._probe_loop)
            hub.spawn(self._control_loop)
            hub.spawn(self._status_loop)

    # ------------------------------------------------------------------ logging
    def event(self, kind, **fields):
        rec = {"ts": round(time.time(), 4), "event": kind}
        rec.update(fields)
        self.logger.info("EVENT %s %s", kind,
                         " ".join(f"{k}={v}" for k, v in fields.items()))
        try:
            with open(EVENT_LOG, "a") as fh:
                fh.write(json.dumps(rec) + "\n")
        except OSError:
            pass

    # ------------------------------------------------------- switch connection
    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def state_change_handler(self, ev):
        dp = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            if dp.id not in self.datapaths:
                self.event("switch_connected", dpid=dp.id)
            self.datapaths[dp.id] = dp
        elif ev.state == DEAD_DISPATCHER:
            self.datapaths.pop(dp.id, None)
            self.event("switch_disconnected", dpid=dp.id)

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        dp = ev.msg.datapath
        parser, ofp = dp.ofproto_parser, dp.ofproto
        # Table-miss: everything unknown goes to the controller.
        self.add_flow(dp, 0, parser.OFPMatch(),
                      [parser.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)])
        self.logger.info("Table-miss rule installed on switch %s", dp.id)

    def add_flow(self, dp, priority, match, actions, idle=0, hard=0, cookie=0, flags=0):
        parser, ofp = dp.ofproto_parser, dp.ofproto
        inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        dp.send_msg(parser.OFPFlowMod(
            datapath=dp, priority=priority, match=match, instructions=inst,
            idle_timeout=idle, hard_timeout=hard, cookie=cookie, flags=flags))

    def delete_flows_by_cookie(self, cookie):
        for dp in self.datapaths.values():
            parser, ofp = dp.ofproto_parser, dp.ofproto
            dp.send_msg(parser.OFPFlowMod(
                datapath=dp, command=ofp.OFPFC_DELETE, table_id=ofp.OFPTT_ALL,
                out_port=ofp.OFPP_ANY, out_group=ofp.OFPG_ANY,
                cookie=cookie, cookie_mask=COOKIE_MASK, match=parser.OFPMatch()))

    # ------------------------------------------------------------ port status
    @set_ev_cls(ofp_event.EventOFPPortStatus, MAIN_DISPATCHER)
    def port_status_handler(self, ev):
        msg = ev.msg
        ofp = msg.datapath.ofproto
        port_no = msg.desc.port_no
        link_down = bool(msg.desc.state & ofp.OFPPS_LINK_DOWN) or msg.reason == ofp.OFPPR_DELETE
        for ip, h in self.hosts.items():
            if h["dpid"] == msg.datapath.id and h["port"] == port_no and ip in self.by_ip:
                s = self.by_ip[ip]
                if link_down and s["status"] == "up":
                    self._mark_down(s, "link_down")
                elif not link_down:
                    self.event("link_up", server=s["name"], port=port_no)
                    self._send_probe(s)    # confirm quickly instead of waiting a tick

    @set_ev_cls(ofp_event.EventOFPFlowRemoved, MAIN_DISPATCHER)
    def flow_removed_handler(self, ev):
        if ev.msg.cookie == COOKIE_BLOCK:
            self.event("block_expired", reason=ev.msg.reason)

    # ------------------------------------------------------------ health logic
    def _mark_down(self, s, reason):
        if s["status"] == "down":
            return
        s["status"] = "down"
        self.event("server_down", server=s["name"], ip=s["ip"], reason=reason)
        self.delete_flows_by_cookie(COOKIE_SERVER + s["idx"])

    def _mark_up(self, s):
        s["misses"] = 0
        if s["status"] != "up":
            s["status"] = "up"
            self.event("server_up", server=s["name"], ip=s["ip"])

    def _send_probe(self, s):
        """Inject a DNS health probe from the controller identity into the switch."""
        if not self.datapaths:
            return
        dp = next(iter(self.datapaths.values()))          # single-switch topology
        parser, ofp = dp.ofproto_parser, dp.ofproto
        host = self.hosts.get(s["ip"])
        dst_mac = host["mac"] if host else "ff:ff:ff:ff:ff:ff"   # broadcast until learned
        out_port = host["port"] if host else ofp.OFPP_FLOOD
        s["probe_seq"] += 1
        payload = json.dumps({"type": "health", "id": s["probe_seq"]}).encode()
        pkt = packet.Packet()
        pkt.add_protocol(ethernet.ethernet(ethertype=ether_types.ETH_TYPE_IP,
                                           dst=dst_mac, src=PROBE_MAC))
        pkt.add_protocol(ipv4.ipv4(src=PROBE_IP, dst=s["ip"], proto=17))
        pkt.add_protocol(udp.udp(src_port=PROBE_SPORT, dst_port=s["port"]))
        pkt.add_protocol(payload)
        pkt.serialize()
        s["outstanding"] = {"seq": s["probe_seq"], "sent": time.monotonic()}
        dp.send_msg(parser.OFPPacketOut(
            datapath=dp, buffer_id=ofp.OFP_NO_BUFFER, in_port=ofp.OFPP_CONTROLLER,
            actions=[parser.OFPActionOutput(out_port)], data=pkt.data))

    def _probe_tick(self):
        for s in self.servers:
            out = s["outstanding"]
            if out is not None:                           # previous probe unanswered
                s["misses"] += 1
                s["outstanding"] = None
                if s["misses"] >= MISS_THRESHOLD:
                    self._mark_down(s, f"{s['misses']}_missed_probes")
            self._send_probe(s)

    def _probe_loop(self):
        while True:
            hub.sleep(PROBE_INTERVAL)
            try:
                self._probe_tick()
            except Exception:                              # never kill the monitor thread
                self.logger.exception("probe tick failed")

    def _handle_probe_reply(self, ip_pkt, payload):
        s = self.by_ip.get(ip_pkt.src)
        if s is None:
            return
        try:
            msg = json.loads(payload.decode())
        except (ValueError, UnicodeDecodeError):
            return
        out = s["outstanding"]
        if msg.get("type") != "health_reply" or out is None or msg.get("id") != out["seq"]:
            return
        s["rtt_ms"] = round((time.monotonic() - out["sent"]) * 1000, 3)
        s["outstanding"] = None
        s["qlen"] = msg.get("qlen", 0)
        s["qps"] = msg.get("qps", 0.0)
        s["assigned"] = 0                                  # fresh load info supersedes estimate
        self._mark_up(s)

    # --------------------------------------------------------------- policies
    def healthy(self):
        return [s for s in self.servers if s["status"] == "up"]

    def choose_server(self):
        live = self.healthy()
        if not live:
            return None
        if self.policy == "roundrobin":
            self.rr_index = (self.rr_index + 1) % len(live)
            return live[self.rr_index]
        if self.policy == "leastloaded":
            return min(live, key=lambda s: (s["qlen"] + s["assigned"], s["idx"]))
        return live[0]                                     # failover: highest priority alive

    # ------------------------------------------------------------- rate limit
    def _over_rate_limit(self, src_ip, now):
        if self.rate_limit <= 0:
            return False
        q = self.query_times[src_ip]
        q.append(now)
        while q and now - q[0] > RATE_WINDOW:
            q.popleft()
        return len(q) > self.rate_limit

    def _block_client(self, dp, src_ip):
        parser, ofp = dp.ofproto_parser, dp.ofproto
        match = parser.OFPMatch(eth_type=0x0800, ip_proto=17, ipv4_src=src_ip, udp_dst=VIP_PORT)
        self.add_flow(dp, 30, match, [], hard=BLOCK_SECONDS, cookie=COOKIE_BLOCK,
                      flags=ofp.OFPFF_SEND_FLOW_REM)         # empty actions == drop
        self.blocked[src_ip] = time.time() + BLOCK_SECONDS
        self.query_times[src_ip].clear()
        self.event("client_blocked", client=src_ip, limit=self.rate_limit,
                   window=RATE_WINDOW, seconds=BLOCK_SECONDS)

    # ------------------------------------------------------------------- ARP
    def _handle_arp(self, dp, in_port, eth, arp_pkt):
        if arp_pkt.opcode != arp.ARP_REQUEST or arp_pkt.dst_ip not in self.arp_proxy:
            return False
        mac = self.arp_proxy[arp_pkt.dst_ip]
        parser, ofp = dp.ofproto_parser, dp.ofproto
        pkt = packet.Packet()
        pkt.add_protocol(ethernet.ethernet(ethertype=ether_types.ETH_TYPE_ARP,
                                           dst=eth.src, src=mac))
        pkt.add_protocol(arp.arp(opcode=arp.ARP_REPLY, src_mac=mac, src_ip=arp_pkt.dst_ip,
                                 dst_mac=arp_pkt.src_mac, dst_ip=arp_pkt.src_ip))
        pkt.serialize()
        dp.send_msg(parser.OFPPacketOut(
            datapath=dp, buffer_id=ofp.OFP_NO_BUFFER, in_port=ofp.OFPP_CONTROLLER,
            actions=[parser.OFPActionOutput(in_port)], data=pkt.data))
        self.counters["arp_proxy_replies"] += 1
        return True

    # ------------------------------------------------------- VIP query handler
    def _handle_vip_query(self, dp, msg, in_port, eth, ip_pkt, udp_pkt):
        parser, ofp = dp.ofproto_parser, dp.ofproto
        now = time.time()
        src = ip_pkt.src
        self.counters["vip_queries"] += 1

        if self._over_rate_limit(src, now):
            if src not in self.blocked or self.blocked[src] < now:
                self._block_client(dp, src)
            self.counters["dropped_rate_limited"] += 1
            return

        server = self.choose_server()
        if server is None:
            self.counters["no_healthy_server"] += 1
            self.event("no_healthy_server", client=src)
            return
        shost = self.hosts.get(server["ip"])
        if shost is None:                      # up implies learned, but be defensive
            return
        server["assigned"] += 1
        self.counters["to_" + server["name"]] += 1

        cookie = COOKIE_SERVER + server["idx"]
        fwd_match = parser.OFPMatch(
            in_port=in_port, eth_type=0x0800, ip_proto=17,
            ipv4_src=src, ipv4_dst=VIP_IP, udp_src=udp_pkt.src_port, udp_dst=VIP_PORT)
        fwd_actions = [
            parser.OFPActionSetField(eth_dst=shost["mac"]),
            parser.OFPActionSetField(ipv4_dst=server["ip"]),
            parser.OFPActionSetField(udp_dst=server["port"]),
            parser.OFPActionOutput(shost["port"]),
        ]
        rev_match = parser.OFPMatch(
            in_port=shost["port"], eth_type=0x0800, ip_proto=17,
            ipv4_src=server["ip"], ipv4_dst=src, udp_src=server["port"],
            udp_dst=udp_pkt.src_port)
        rev_actions = [
            parser.OFPActionSetField(eth_src=VIP_MAC),
            parser.OFPActionSetField(ipv4_src=VIP_IP),
            parser.OFPActionSetField(udp_src=VIP_PORT),
            parser.OFPActionOutput(in_port),
        ]
        self.add_flow(dp, 20, fwd_match, fwd_actions, idle=FLOW_IDLE, cookie=cookie)
        self.add_flow(dp, 20, rev_match, rev_actions, idle=FLOW_IDLE, cookie=cookie)
        dp.send_msg(parser.OFPPacketOut(
            datapath=dp, buffer_id=ofp.OFP_NO_BUFFER, in_port=in_port,
            actions=fwd_actions, data=msg.data))
        self.logger.info("REDIRECT %s:%s -> VIP -> %s (%s, policy=%s)",
                         src, udp_pkt.src_port, server["name"], server["ip"], self.policy)

    # --------------------------------------------------------------- packet-in
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg = ev.msg
        dp = msg.datapath
        parser, ofp = dp.ofproto_parser, dp.ofproto
        in_port = msg.match["in_port"]
        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth is None or eth.ethertype in (0x88CC, ether_types.ETH_TYPE_IPV6):
            return

        dpid = dp.id
        self.mac_to_port.setdefault(dpid, {})[eth.src] = in_port
        arp_pkt = pkt.get_protocol(arp.arp)
        ip_pkt = pkt.get_protocol(ipv4.ipv4)
        if arp_pkt is not None:
            self.hosts[arp_pkt.src_ip] = {"mac": eth.src, "dpid": dpid, "port": in_port}
        elif ip_pkt is not None and eth.src not in (VIP_MAC, PROBE_MAC):
            self.hosts[ip_pkt.src] = {"mac": eth.src, "dpid": dpid, "port": in_port}

        if arp_pkt is not None and self._handle_arp(dp, in_port, eth, arp_pkt):
            return

        udp_pkt = pkt.get_protocol(udp.udp)
        if ip_pkt is not None and udp_pkt is not None:
            if ip_pkt.dst == PROBE_IP:                    # reply to a controller probe
                payload = pkt.protocols[-1] if isinstance(pkt.protocols[-1], (bytes, bytearray)) else b""
                self._handle_probe_reply(ip_pkt, bytes(payload))
                return
            if (self.mode == "sdn" and ip_pkt.dst == VIP_IP and udp_pkt.dst_port == VIP_PORT):
                self._handle_vip_query(dp, msg, in_port, eth, ip_pkt, udp_pkt)
                return

        # ---- ordinary L2 learning-switch forwarding (baseline / non-DNS traffic)
        out_port = self.mac_to_port[dpid].get(eth.dst, ofp.OFPP_FLOOD)
        actions = [parser.OFPActionOutput(out_port)]
        if out_port != ofp.OFPP_FLOOD:
            self.add_flow(dp, 1, parser.OFPMatch(in_port=in_port, eth_dst=eth.dst), actions)
        data = msg.data if msg.buffer_id == ofp.OFP_NO_BUFFER else None
        dp.send_msg(parser.OFPPacketOut(datapath=dp, buffer_id=msg.buffer_id,
                                        in_port=in_port, actions=actions, data=data))

    # -------------------------------------------------------- control + status
    def status(self):
        return {
            "mode": self.mode, "policy": self.policy, "rate_limit": self.rate_limit,
            "servers": [{k: s[k] for k in ("name", "ip", "status", "misses", "qlen", "qps",
                                           "rtt_ms", "assigned")} for s in self.servers],
            "blocked": {ip: round(t - time.time(), 1) for ip, t in self.blocked.items()
                        if t > time.time()},
            "counters": dict(self.counters),
        }

    def apply_command(self, cmd):
        op = cmd.get("cmd")
        if op == "status":
            return {"ok": True, "status": self.status()}
        if op == "set":
            if "mode" in cmd:
                if cmd["mode"] not in ("sdn", "static"):
                    return {"ok": False, "error": "mode must be sdn|static"}
                self.mode = cmd["mode"]
            if "policy" in cmd:
                if cmd["policy"] not in POLICIES:
                    return {"ok": False, "error": f"policy must be one of {POLICIES}"}
                self.policy = cmd["policy"]
            if "rate_limit" in cmd:
                self.rate_limit = int(cmd["rate_limit"])
            self.flush_dynamic_flows()
            self.counters.clear()
            self.event("config_changed", mode=self.mode, policy=self.policy,
                       rate_limit=self.rate_limit)
            return {"ok": True, "status": self.status()}
        if op == "unblock":
            self.blocked.clear()
            self.delete_flows_by_cookie(COOKIE_BLOCK)
            return {"ok": True}
        return {"ok": False, "error": "unknown cmd"}

    def flush_dynamic_flows(self):
        """Flush every redirect/block flow so a new policy takes effect immediately."""
        for s in self.servers:
            self.delete_flows_by_cookie(COOKIE_SERVER + s["idx"])
        self.delete_flows_by_cookie(COOKIE_BLOCK)
        self.blocked.clear()
        self.query_times.clear()

    def _control_loop(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", CTL_PORT))
        except OSError as exc:
            self.logger.warning("control socket not available: %s", exc)
            return
        while True:
            data, addr = sock.recvfrom(4096)
            try:
                reply = self.apply_command(json.loads(data.decode()))
            except Exception as exc:                       # keep control loop alive
                reply = {"ok": False, "error": str(exc)}
            sock.sendto(json.dumps(reply).encode(), addr)

    def _status_loop(self):
        while True:
            hub.sleep(5)
            st = self.status()
            self.logger.info("STATUS mode=%s policy=%s servers=%s counters=%s",
                             st["mode"], st["policy"],
                             {s["name"]: s["status"] for s in st["servers"]}, st["counters"])
