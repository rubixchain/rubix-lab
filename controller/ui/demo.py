#!/usr/bin/env python3
"""
demo.py - the Rubix lab web UI on a fake fleet, for working on the UI away
from the lab (Windows or Linux, nothing to install).

    python demo.py

then open http://127.0.0.1:8470. Every page works: the fleet is
demofleet/fakefleet.py, and a run is the real test_runner.py on that fake
fleet (demofleet/demo_runner.py). Everything it writes - its database,
reports, logs, run lock, ledger and limits - stays in controller/ui/demo-data/,
so it never mixes with the real lab's files. Delete that folder to start over.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from labui import Config, DEMO_DIR, UI_DIR, main  # noqa: E402

sys.path.insert(0, DEMO_DIR)

PORT = 8470
DATA = os.path.join(UI_DIR, "demo-data")


def config():
    return Config(demo=True, port=PORT, bind="127.0.0.1", data_dir=DATA,
                  reports_dir=os.path.join(DATA, "reports"),
                  lock_path=os.path.join(DATA, "fleet-run.lock"),
                  ledger_path=os.path.join(DATA, "fleet-ledger.json"),
                  limits_path=os.path.join(DATA, "lab-limits.json"),
                  runner=[sys.executable, "-u", os.path.join(DEMO_DIR, "demo_runner.py"), DATA])


if __name__ == "__main__":
    from source_demo import DemoSource
    cfg = config()
    main.run(cfg, DemoSource(cfg))
