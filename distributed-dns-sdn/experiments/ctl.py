"""Talk to the running controller over its UDP control socket (127.0.0.1:9099).

    python3 experiments/ctl.py status
    python3 experiments/ctl.py set --mode sdn --policy roundrobin --rate-limit 200
    python3 experiments/ctl.py unblock
"""
import argparse
import json
import os
import socket

CTL_PORT = int(os.environ.get("DNS_SDN_CTL_PORT", "9099"))


def send(cmd, timeout=2.0):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(json.dumps(cmd).encode(), ("127.0.0.1", CTL_PORT))
        return json.loads(s.recvfrom(65535)[0].decode())
    finally:
        s.close()


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="op", required=True)
    sub.add_parser("status")
    sub.add_parser("unblock")
    st = sub.add_parser("set")
    st.add_argument("--mode", choices=["sdn", "static"])
    st.add_argument("--policy", choices=["failover", "roundrobin", "leastloaded"])
    st.add_argument("--rate-limit", type=int)
    a = ap.parse_args()
    cmd = {"cmd": a.op}
    if a.op == "set":
        for k in ("mode", "policy"):
            if getattr(a, k):
                cmd[k] = getattr(a, k)
        if a.rate_limit is not None:
            cmd["rate_limit"] = a.rate_limit
    print(json.dumps(send(cmd), indent=2))


if __name__ == "__main__":
    main()
