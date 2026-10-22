# Rubix Lab web UI

The fleet, test runs and their reports in one browser page, served by the
controller. Open it from any lab machine: **http://192.168.1.120:8470**.

| Page | Shows |
|---|---|
| Fleet | every machine and node: up, peer ID, DIDs held vs the layout, quorum list, build; the faucet pair (`faucet_ready`, balances); the fullnodes (accepted / rejected, rejection reasons grouped, transactions they disagree on) |
| Run builder | the catalogue cases with their text, the runner's scale knobs, and the plan for a run - units, nodes, quorum funding, the most it can draw from the faucet - before Start |
| Live run | the run as it goes: units and participants, every operation, p50/p95/max per case, refusals grouped (known product bugs tagged), DB checks, fullnode verdicts, the console. Reload it or open it on another machine: it picks the run up again |
| Reports | every run - from the UI, from a terminal, and the older reports in `reports/` - with its JSON, PDF, evidence and log |

## Starting it on the controller

    cd ~/Desktop/rubix/rubix-lab && git pull
    python3 ~/Desktop/rubix/rubix-lab/controller/ui/server.py

Ctrl+C stops the UI; a test run that is going keeps going, and the UI picks it
up again when it next starts. To keep it running after the terminal closes:

    nohup python3 ~/Desktop/rubix/rubix-lab/controller/ui/server.py > ~/Desktop/rubix/rubix-lab/controller/ui/server.log 2>&1 &

and to stop it: `pkill -f controller/ui/server.py`.

Needs only what the runner needs (`python3-psycopg2`, `python3-reportlab`).
Nothing to configure: hosts.txt and the node layout are read through
`controller/fleet.py`, as every other tool does.

## How it works

- **Runs** are `test_runner.py`, started as its own process with
  `--events reports/events/<run>.jsonl` (`full-test/run_events.py`). Without
  `--events` the runner behaves exactly as before.
- **One run at a time**: `test_runner.py`, `smoke_test.py` and
  `custom/unpledged_spend.py` take the fleet's run lock
  (`full-test/run_lock.py`) whether started from a terminal or from the UI, and
  refuse to start while another run holds it. The lock goes with the process,
  so a crashed run never leaves it behind.
- **Polling** is done once, by the UI's backend, never by the browsers:
  liveness 30 s, DIDs and quorum lists 5 min, faucet 1 min, fullnodes 2 min.
  Slower during a run, and nothing at all while an exclusive unit (QCAP-01, the
  audits) runs. At most one database connection per node.
- **Stop** ends the runner's process. Transfers it had already sent keep going
  on the nodes; the UI then checks the participants for RBT left Locked.
- **History** is in `controller/ui/lab-ui.sqlite` (local, not in git). Past
  reports are imported from `reports/` when the UI starts and every minute.
- The UI never writes to a node's database, never restarts, wipes or deploys a
  node, and never creates a DID.

## Demo (away from the lab)

    python controller/ui/demo.py

then open http://127.0.0.1:8470. The same UI on a fake fleet
(`demofleet/fakefleet.py`); a run is the real `test_runner.py` on that fake
fleet. Everything it writes stays in `controller/ui/demo-data/` (delete the
folder to start over), and nothing in a demo run can reach a lab machine.
