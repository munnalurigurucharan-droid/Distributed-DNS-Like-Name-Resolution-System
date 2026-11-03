"""Shared helpers for the DNS-like UDP service: addressing, wire format, zone loading.

Wire format: one JSON object per UDP datagram.
  query    {"type":"query","id":<int>,"domain":"x.local"}
  response {"type":"response","id":<int>,"domain":..,"status":"success|not_found|invalid_query",
            "ip":..,"ttl":..,"server":"Server A"}
  health   {"type":"health","id":<int>}  ->  {"type":"health_reply","id":..,"status":"ok","server":..,
            "qlen":<queue length>,"served":<int>,"dropped":<int>,"qps":<float>}
  register {"type":"register","domain":..,"ip":..,"ttl":..}   (admin, replicated to peers)
  delete   {"type":"delete","domain":..}
"""
import json
import os
import random

# Virtual IP that clients use in SDN mode. The controller rewrites it to a healthy backend.
VIP_IP = os.environ.get("DNS_VIP_IP", "10.0.0.100")
VIP_PORT = int(os.environ.get("DNS_VIP_PORT", "5300"))

_DEFAULT_BACKENDS = "10.0.0.20:5001:Server A,10.0.0.30:5002:Server B,10.0.0.40:5003:Server C"


def parse_backends(text):
    out = []
    for item in text.split(","):
        host, port, name = item.strip().split(":", 2)
        out.append((host, int(port), name))
    return out


# Real DNS servers, used directly by the static baseline ("direct" mode).
BACKENDS = parse_backends(os.environ.get("DNS_BACKENDS", _DEFAULT_BACKENDS))

DEFAULT_ZONE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "zones", "zone.json")
MAX_DGRAM = 2048


def new_txid():
    return random.getrandbits(32)


def encode(obj):
    return json.dumps(obj, separators=(",", ":")).encode("utf-8")


def decode(data):
    obj = json.loads(data.decode("utf-8"))
    if not isinstance(obj, dict):
        raise ValueError("message is not a JSON object")
    return obj


def normalize(domain):
    return str(domain).strip().lower().rstrip(".")


def load_zone(path=None):
    """Return {domain: (ip, ttl)}. Values may be 'ip' strings or {"ip":..,"ttl":..}."""
    path = path or DEFAULT_ZONE
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    zone = {}
    for name, val in raw.items():
        if isinstance(val, str):
            zone[normalize(name)] = (val, 60)
        else:
            zone[normalize(name)] = (val["ip"], int(val.get("ttl", 60)))
    return zone
