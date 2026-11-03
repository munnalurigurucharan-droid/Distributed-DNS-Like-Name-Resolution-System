# Changes from your original project

REPLACED (overwrite your files)
- app/sdn_controller.py   passive observer -> VIP redirect, health probes, policies, rate limit, control socket
- app/client.py           txid validation, TTL/negative cache, retries, --mode sdn|direct, one-shot --query
- topology.py             3 servers + 2 clients + attacker, TCLink, checksum-offload fix (start_net)
- run_controller.py       now also loads os_ken.controller.ofp_handler (opens port 6653)
- README.md               rewritten (no hard-coded /home/nithya paths)

NEW
- app/dns_server.py (unified), app/dns_common.py, app/loadgen.py, app/flood.py
- zones/zone.json, experiments/{ctl,run_experiments,analyze}.py, tests/, requirements.txt, .gitignore

DELETE
- app/dns_server_b.py        (use dns_server.py --name "Server B" --port 5002)
- app/sdn_controller_backup.py
- .venv/ and __pycache__/    (do not commit; use requirements.txt)
