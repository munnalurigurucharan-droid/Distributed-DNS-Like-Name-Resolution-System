# Distributed DNS-Like Name Resolution System Using SDN

**Project 22 – Computer Networks**

## Overview

This project implements a simplified DNS-like name resolution system using UDP sockets and Software-Defined Networking (SDN). It allows clients to request the IP address associated with a domain name. An SDN controller manages how requests are forwarded to multiple name-resolution servers.

## Key Features

- UDP-based domain name resolution
- Multiple DNS-like servers with domain-to-IP mappings
- SDN-based request forwarding
- Round-robin, least-loaded, and failover server-selection policies
- Client-side caching, timeouts, and retries
- Server health monitoring and failure handling
- Performance testing and lookup-latency analysis

## System Architecture

The system consists of UDP clients, multiple DNS-like servers, an SDN controller, and a Mininet network with an Open vSwitch. Clients send lookup requests through a virtual service address. The controller selects an available server and manages request forwarding. If a server becomes unavailable, requests can be redirected to another healthy server.

## Technologies Used

- Python
- UDP sockets
- Software-Defined Networking (SDN)
- Mininet
- Open vSwitch
- OpenFlow

## Project Structure

- **app/** – Client, DNS-like server, controller, and traffic-generation modules
- **experiments/** – Experiment execution, controller configuration, and result analysis
- **tests/** – Automated tests
- **zones/** – Initial domain-to-IP records
- **results/** – Output from experiments
- **topology.py** – Mininet network topology
- **run_controller.py** – Controller startup module

## Requirements

- Ubuntu Linux (recommended)
- Python 3
- Mininet
- Open vSwitch
- Required Python packages listed in `requirements.txt`

## Running the Project

Set up the project in an Ubuntu environment, install the required system and Python dependencies, and start the SDN controller and Mininet topology. Once the network is running, start the DNS-like servers and send domain lookup requests from a client. The experiment utilities can be used to change server-selection policies, test server failures, and measure performance.

Refer to the project files and experiment modules for the available execution options.

## Project Objective

The objective is to demonstrate how SDN can manage DNS-like request forwarding across multiple servers while supporting caching, load distribution, fault handling, and performance evaluation.
