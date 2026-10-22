#!/usr/bin/env python3
"""demo_runner.py - test_runner.py on the fake fleet, for the web UI's demo.

    python demo_runner.py <demo data folder> <test_runner.py arguments>

The demo UI (controller/ui/demo.py) starts this instead of test_runner.py, with
the same arguments. It points the client libraries at fakefleet.py and every
file the runner writes (reports, log, ledger, limits, the run lock) at the demo
folder, then calls the real test_runner.main(). Never used on the fleet.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
UI = os.path.dirname(HERE)
CONTROLLER = os.path.dirname(UI)
for p in (CONTROLLER, os.path.join(CONTROLLER, "test-plan", "master"),
          os.path.join(CONTROLLER, "test-plan", "full-test"), HERE):
    if p not in sys.path:
        sys.path.insert(0, p)


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: demo_runner.py <demo data folder> [test_runner.py arguments]")
    data_dir = os.path.abspath(sys.argv[1])
    import fakefleet
    fake = fakefleet.FakeFleet(os.path.join(data_dir, "fake-fleet.json"), writer=True)
    fakefleet.install_runner(fake, data_dir)
    import test_runner
    sys.argv = [os.path.join(CONTROLLER, "test-plan", "full-test", "test_runner.py")] + sys.argv[2:]
    test_runner.main()


if __name__ == "__main__":
    main()
