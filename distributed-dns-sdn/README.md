# Distributed DNS-Like Name Resolution with SDN (Project 22)

UDP name-resolution service whose requests are **dynamically forwarded, load-balanced,
failed-over and rate-limited by an OpenFlow 1.3 SDN controller** (OS-Ken) in Mininet.

## 1\. Architecture

```
 h1 client1  10.0.0.10 ─┐                      ┌─ h2 Server A 10.0.0.20 :5001
 h5 client2  10.0.0.50 ─┼── s1 (OVS, OF 1.3) ──┼─ h3 Server B 10.0.0.30 :5002
 h6 attacker 10.0.0.60 ─┘        │             └─ h4 Server C 10.0.0.40 :5003
                          c0: OS-Ken controller (127.0.0.1:6653, control socket :9099)
 Clients query the virtual IP  10.0.0.100:5300  (answered by the controller via ARP proxy)
```

Flow of one lookup in SDN mode

1. Client sends a UDP query to the VIP. The switch has no flow, so it goes to the controller (packet-in).
2. Controller applies rate limiting, then the policy (failover / roundrobin / leastloaded) over servers
currently **UP**, and installs two flows: forward (rewrite dst MAC/IP/UDP port to the chosen server) and
reverse (rewrite src back to the VIP). The first packet is forwarded with a packet-out.
3. The server answers; the client sees a reply from the VIP and never learns which server answered
(the response's `server` field shows it, for demo purposes).

Health monitoring: every 0.5 s the controller injects a DNS `health` probe (identity 10.0.0.254) toward
each server. 3 missed probes ⇒ server DOWN, its flows are deleted, and the next query is redirected.
A link-down PortStatus message marks a server DOWN immediately. Replies also carry the server's queue
length, which the `leastloaded` policy uses.

|Requirement|Where|
|-|-|
|UDP name-resolution queries|`app/client.py`, `app/dns\_server.py` (JSON over UDP, txid-checked)|
|Name→IP mappings|`zones/zone.json`; runtime `register`/`delete` messages, replicated to `--peers`|
|Multiple resolution servers|Servers A, B, C (one program, `--name --port`)|
|Caching|Client TTL cache (TTL from server, negative caching, LRU)|
|Timeouts|Per-attempt timeout + retries in client; stray/late replies ignored|
|Failure detection|Controller active probes + link-down events (`server\_down` events)|
|Redirect requests|Controller rewrites flows to a healthy server (also load balancing)|
|Lookup latency|`loadgen.py` CSV per query; `experiments/analyze.py` p50/p95/p99|
|Dynamic SDN|health-aware redirect, load balancing, rate-limit/blocking, recovery on restart|

## 2\. Files

```
topology.py                 Mininet topology (3 servers, 2 clients, 1 attacker)
run\_controller.py           starts the controller (works with `pip install os-ken`)
app/dns\_common.py           addresses, wire format, zone loader
app/dns\_server.py           DNS server (replaces dns\_server.py + dns\_server\_b.py)
app/client.py               client: cache, retries, --mode sdn|direct
app/loadgen.py, flood.py    measurement load generator, DNS flood (attack)
app/sdn\_controller.py       the SDN controller
experiments/ctl.py          read status / change policy of the running controller
experiments/run\_experiments.py   runs all evaluation scenarios in Mininet
experiments/analyze.py      results/\*.csv -> results/summary.md + PNG plots
tests/                      offline unit tests (UDP app + controller logic)
```

## 3\. Setup

```bash
sudo apt install mininet openvswitch-switch ethtool python3-pip
pip install -r requirements.txt            # os-ken, matplotlib
```

Terminal 1 – controller (leave running):

```bash
python3 run\_controller.py          # or: cd app \&\& osken-manager sdn\_controller.py
```

Terminal 2 – Mininet:

```bash
sudo python3 topology.py           # add DNS\_SDN\_DATAPATH=user if OVS has no kernel module
```

## 4\. Manual demo (Mininet CLI)

```text
mininet> h2 python3 app/dns\_server.py --name "Server A" --port 5001 --quiet \&
mininet> h3 python3 app/dns\_server.py --name "Server B" --port 5002 --quiet \&
mininet> h4 python3 app/dns\_server.py --name "Server C" --port 5003 --quiet \&
mininet> h1 python3 app/client.py --query example.com        # SDN mode via VIP
mininet> h1 python3 app/client.py --mode direct --query example.com   # static baseline
mininet> h2 pkill -f dns\_server.py                           # crash Server A -> redirect to B
mininet> link s1 h2 down                                     # link failure
mininet> h6 python3 app/flood.py --rate 800 --duration 10    # flood -> blocked by controller
```

Watch the controller terminal (`REDIRECT`, `server\_down`, `client\_blocked`) or run
`python3 experiments/ctl.py status`. Change policy live:
`python3 experiments/ctl.py set --policy roundrobin` (`failover|roundrobin|leastloaded`).

## 5\. Tests

Offline (no Mininet):

```bash
python3 -m unittest tests.test\_dns\_udp -v
DNS\_SDN\_NO\_THREADS=1 DNS\_SDN\_EVENT\_LOG=/dev/null python3 -m unittest tests.test\_controller -v
```

In Mininet: `sudo python3 experiments/run\_experiments.py --quick` (smoke) or without `--quick` (full).

## 6\. Evaluation

Controller must be running. Then:

```bash
sudo python3 experiments/run\_experiments.py      # \~8 minutes, writes results/\*.csv|json
python3 experiments/analyze.py                   # results/summary.md + PNG plots
```

Each scenario compares the **static baseline** (controller in `static` mode = plain learning switch;
client talks directly to A→B→C and fails over by itself) with the **SDN approach**:

|id|scenario|what it shows|
|-|-|-|
|e1|no faults|latency cost of per-query controller decisions (first-packet overhead)|
|e1c|cache on/off, Zipf names|cache hit ratio and mean latency|
|e2 / e2l|server crash / link down at t=10 s, restore at t=20 s|affected queries, recovery time, detection time|
|e3|overload (100 qps/server, 140 qps offered)|static piles onto A; SDN spreads load|
|e4|DNS flood from h6|rate limiting blocks the attacker, legit latency protected|
|e5|10 % query loss on Server A|retries needed: static vs least-loaded|

Paste your own numbers and the PNGs from `results/` into your report.

## 7\. Configuration (environment variables of the controller)

`DNS\_SDN\_MODE`, `DNS\_SDN\_POLICY`, `DNS\_SDN\_PROBE\_INTERVAL` (0.5 s), `DNS\_SDN\_MISS\_THRESHOLD` (3),
`DNS\_SDN\_RATE\_LIMIT` (200 queries/s per client), `DNS\_SDN\_BLOCK\_SECONDS` (10), `DNS\_SDN\_EVENT\_LOG`.
Failure detection time ≈ interval × threshold; lowering it speeds recovery but risks false positives.

## 8\. Limitations

* Every query is decided by the controller (per-flow packet-in), which adds \~1–2 ms and bounds throughput.
* Single switch; no multi-path rerouting. Rate limiting counts new flows (new source ports); a flood that
reuses one source port is not counted after its first packet.
* Servers replicate registrations best-effort over UDP (no consistency protocol, no authentication).
* Client cache is in memory only.

