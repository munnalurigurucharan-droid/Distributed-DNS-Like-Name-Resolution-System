"""Minimal DNS flood used to demonstrate SDN rate limiting / blocking.

Sends queries to the VIP at a high rate from a fresh UDP source port each time (as a
real resolver flood would) and does not wait for answers.

    python3 flood.py --rate 1500 --duration 10
"""
import argparse
import socket
import time

import dns_common as dc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default=f"{dc.VIP_IP}:{dc.VIP_PORT}")
    ap.add_argument("--rate", type=float, default=1000.0)
    ap.add_argument("--duration", type=float, default=10.0)
    args = ap.parse_args()
    host, port = args.target.rsplit(":", 1)
    payload = dc.encode({"type": "query", "id": 1, "domain": "example.com"})
    sent, start = 0, time.time()
    while time.time() - start < args.duration:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.sendto(payload, (host, int(port)))
        s.close()
        sent += 1
        ahead = start + sent / args.rate - time.time()
        if ahead > 0:
            time.sleep(ahead)
    print(f"flood: sent {sent} queries in {time.time() - start:.1f}s")


if __name__ == "__main__":
    main()
