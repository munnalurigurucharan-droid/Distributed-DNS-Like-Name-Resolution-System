# Project Architecture & Design

## 1. Project Overview

This project implements a distributed DNS-like name-resolution system
using UDP sockets and Software-Defined Networking (SDN). Multiple DNS
servers maintain name-to-IP mappings, while an SDN controller
dynamically directs client queries to available servers.

## 2. High-Level Architecture

The system contains five main parts:

-   **Clients:** Send name-resolution queries to a virtual DNS address.
    They support caching, timeouts, retries, and direct or SDN-based
    operation.
-   **DNS Servers:** Three independent servers receive queries, look up
    domain names, and return responses. They support runtime
    registration and deletion of records.
-   **SDN Controller:** Monitors server health, applies traffic
    policies, selects a suitable server, and manages forwarding rules.
-   **OpenFlow Switch:** Connects clients and servers. It communicates
    with the controller and applies the forwarding rules it receives.
-   **Zone Database:** Stores the initial domain-to-IP mappings used by
    the DNS servers.

## 3. Network Design

The Mininet topology uses one OpenFlow 1.3 switch connected to:

-   Three DNS servers: Server A, Server B, and Server C.
-   Two client hosts.
-   One additional host used to generate traffic for rate-limit testing.
-   One SDN controller connected to the switch.

Clients send SDN-mode queries to a virtual IP address rather than
choosing a backend server themselves. The controller selects the actual
DNS server.

## 4. Query Processing Flow

1.  A client sends a UDP query to the virtual DNS address.
2.  The switch forwards the initial packet to the SDN controller when a
    matching forwarding rule is not available.
3.  The controller checks the client traffic and identifies DNS servers
    that are currently available.
4.  Based on the configured policy, the controller selects a server and
    installs forwarding rules.
5.  The selected DNS server processes the query and sends its response.
6.  The forwarding rules make the response appear to the client as
    coming from the virtual DNS address.

## 5. Server Monitoring and Failure Handling

The controller periodically sends health probes to each DNS server. If a
server stops responding beyond the configured threshold, it is marked as
unavailable and its related forwarding rules are removed. New queries
are then directed to another available server. Link-down events can also
indicate server failure.

## 6. Traffic Management

The controller supports three server-selection policies:

-   **Failover:** Selects an available server according to the
    configured priority.
-   **Round Robin:** Distributes new client flows across available
    servers in rotation.
-   **Least Loaded:** Prefers the available server reporting the
    smallest queue.

The controller also applies client rate limits and can temporarily block
clients that exceed the configured limit.

## 7. Main Software Modules

  -----------------------------------------------------------------------
  Module                              Responsibility
  ----------------------------------- -----------------------------------
  `client.py`                         Sends queries and handles caching,
                                      timeouts, and retries

  `dns_server.py`                     Runs the DNS-like UDP server and
                                      handles records

  `dns_common.py`                     Shared message format, addresses,
                                      and zone-loading utilities

  `sdn_controller.py`                 Server monitoring, policy
                                      decisions, and flow management

  `topology.py`                       Builds the Mininet network

  `loadgen.py` and `flood.py`         Generate test traffic

  `experiments/`                      Runs performance and fault
                                      scenarios and analyzes results

  `tests/`                            Contains tests for DNS
                                      communication and controller
                                      behavior
  -----------------------------------------------------------------------

## 8. Design Summary

The design separates name-resolution logic from network-forwarding
decisions. DNS servers handle queries and records, while the SDN
controller manages how traffic reaches those servers. This separation
allows the system to distribute requests, respond to server failures,
apply traffic controls, and measure performance in a controlled network
environment.
