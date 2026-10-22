"""labui - the backend of the Rubix lab web UI (controller/ui).

It reads the fleet through the same code the lab's tools use - controller/fleet.py,
full-test/rubix_client.py and db_client.py, the case modules in master/ - and
starts test_runner.py as its own process for every run. Nothing here writes to
a node's database, restarts, wipes or deploys a node, or creates a DID.

The folders it needs are put on sys.path here, so `import fleet`,
`import rubix_client`, `import test_runner` work from any module of the package.
"""

import os
import sys

UI_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTROLLER = os.path.dirname(UI_DIR)
REPO = os.path.dirname(CONTROLLER)
TEST_PLAN = os.path.join(CONTROLLER, "test-plan")
FULL_TEST = os.path.join(TEST_PLAN, "full-test")
MASTER = os.path.join(TEST_PLAN, "master")
STATIC = os.path.join(UI_DIR, "static")
DEMO_DIR = os.path.join(UI_DIR, "demofleet")

for _p in (CONTROLLER, MASTER, FULL_TEST):
    if _p not in sys.path:
        sys.path.insert(0, _p)


class Config(object):
    """Where one instance of the UI keeps things, and how it starts a run.
    server.py builds the real one, demo.py the demo's."""

    def __init__(self, demo, port, bind, data_dir, reports_dir, lock_path, ledger_path,
                 limits_path, runner):
        self.demo = demo
        self.port = port
        self.bind = bind
        self.data_dir = data_dir
        self.reports_dir = reports_dir
        self.events_dir = os.path.join(reports_dir, "events")
        self.lock_path = lock_path
        self.ledger_path = ledger_path
        self.limits_path = limits_path
        self.runner = runner            # argv before the runner's own arguments
        self.db_path = os.path.join(data_dir, "lab-ui-demo.sqlite" if demo else "lab-ui.sqlite")
        for d in (data_dir, self.events_dir):
            os.makedirs(d, exist_ok=True)
