"""Mininet topology: one OpenFlow 1.3 switch, 3 DNS servers, 2 clients, 1 attacker.

    h1 client 1   10.0.0.10        h2 Server A 10.0.0.20 (udp/5001)
    h5 client 2   10.0.0.50        h3 Server B 10.0.0.30 (udp/5002)
    h6 attacker   10.0.0.60        h4 Server C 10.0.0.40 (udp/5003)
    VIP (virtual, answered by the controller): 10.0.0.100 udp/5300

Manual use:   sudo python3 topology.py            (controller must already be running)
"""
import os

from mininet.cli import CLI
from mininet.link import TCLink
from mininet.log import setLogLevel
from mininet.net import Mininet
from mininet.node import OVSSwitch, RemoteController

HOSTS = {
    "h1": "10.0.0.10/24", "h2": "10.0.0.20/24", "h3": "10.0.0.30/24",
    "h4": "10.0.0.40/24", "h5": "10.0.0.50/24", "h6": "10.0.0.60/24",
}


def build_net(controller_ip="127.0.0.1", controller_port=6653):
    net = Mininet(controller=None, switch=OVSSwitch, link=TCLink, autoSetMacs=True)
    net.addController("c0", controller=RemoteController, ip=controller_ip, port=controller_port)
    sw_opts = {}
    if os.environ.get("DNS_SDN_DATAPATH"):          # e.g. "user" on machines without the OVS kernel module
        sw_opts["datapath"] = os.environ["DNS_SDN_DATAPATH"]
    s1 = net.addSwitch("s1", protocols="OpenFlow13", failMode="secure", **sw_opts)
    for name, ip in HOSTS.items():
        net.addLink(net.addHost(name, ip=ip), s1)
    return net


def start_net(net):
    """Start the network and disable NIC checksum offload on the hosts.

    The controller rewrites dst IP/port of DNS queries. With tx checksum offload the host
    kernel leaves a partial UDP checksum that is wrong after the rewrite, so the backend
    would silently drop the packet. Turning offload off makes the rewrite reliable.
    """
    net.start()
    for h in net.hosts:
        h.cmd(f"ethtool -K {h.name}-eth0 tx off rx off 2>/dev/null")


def main():
    net = build_net()
    start_net(net)
    print("\nNetwork started.  Servers: h2/h3/h4   Clients: h1/h5   Attacker: h6")
    print("VIP 10.0.0.100:5300.  Try:  pingall   |   h2 python3 app/dns_server.py --name 'Server A' --port 5001 &")
    CLI(net)
    net.stop()


if __name__ == "__main__":
    setLogLevel("info")
    main()
