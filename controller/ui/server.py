#!/usr/bin/env python3
"""
server.py - the Rubix lab web UI, on the real fleet. Runs on the controller.

    python3 ~/Desktop/rubix/rubix-lab/controller/ui/server.py

then open http://192.168.1.120:8470 from any lab machine. Everything is set in
the page; there is nothing to configure here.

It reads the fleet through controller/fleet.py and the test-plan clients, and
runs test_runner.py as its own process for each run, so a run from the UI is
the same as one from a terminal - and a terminal run is shown in the UI too.
Its history is in controller/ui/lab-ui.sqlite (local, not in git); past runs
are imported from reports/ when it starts.

Needs only what the runner needs: python3, python3-psycopg2 (fullnode panel
and every run's database check), python3-reportlab (PDF reports).
demo.py is the same UI on a fake fleet, for working away from the lab.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from labui import Config, FULL_TEST, REPO, UI_DIR, main  # noqa: E402

PORT = 8470


def config():
    import case_helpers
    import run_lock
    import test_runner
    return Config(demo=False, port=PORT, bind="0.0.0.0", data_dir=UI_DIR,
                  reports_dir=os.path.join(REPO, "reports"), lock_path=run_lock.LOCK_PATH,
                  ledger_path=test_runner.LEDGER_PATH, limits_path=case_helpers.LIMITS_PATH,
                  runner=[sys.executable, "-u", os.path.join(FULL_TEST, "test_runner.py")])


if __name__ == "__main__":
    from labui.source_real import RealSource
    cfg = config()
    main.run(cfg, RealSource(cfg))
