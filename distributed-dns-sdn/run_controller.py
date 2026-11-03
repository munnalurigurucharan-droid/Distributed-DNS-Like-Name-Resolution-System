#!/usr/bin/env python3
"""Start the SDN controller without the `osken-manager` command.

Works with `pip install os-ken` (which ships no osken-manager script):

    python3 run_controller.py

It loads the OpenFlow handler (listens on TCP 6653) plus app/sdn_controller.py.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "app"))

from os_ken import cfg                      # noqa: E402
from os_ken.base import app_manager         # noqa: E402

cfg.CONF(args=[], project="os_ken")
app_manager.AppManager.run_apps([
    "os_ken.controller.ofp_handler",
    os.path.join(HERE, "app", "sdn_controller.py"),
])
