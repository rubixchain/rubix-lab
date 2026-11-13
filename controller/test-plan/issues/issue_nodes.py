"""issue_nodes.py - what happens to a node, and its machine, when its IPFS daemon
dies and Rubix starts it again. Runs last: it leaves one machine's nodes deaf
until its own last step restarts them all. FAIL = the issue reproduced.
"""

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import issue_kit as kit_mod
from issue_kit import ch, rc, steps

BACK_WAIT = 120         # seconds Rubix gets to start the node's IPFS again (it checks every 10s)

PURPOSE = {
    "IPFS-RESTART":
        "When a node's IPFS daemon dies, Rubix starts it again (core/ipfs_recovery.go) but never "
        "registers the node's p2p listener on the new daemon, so no peer can reach the node until it "
        "restarts - its API keeps answering. A payment that needs it is refused with a message that "
        "says the DID does not exist (core/ipfsport/peer.go returns a nil error after its retries). "
        "Stopping that node afterwards can kill every IPFS daemon on the machine: the restart never "
        "recorded the new daemon's PID, and shutdown falls back to 'killall -9 ipfs' "
        "(core/shutdown_manager.go:280-300).",
}

STEPS = {
    "IPFS-RESTART": [
        ("S1", "Set-up",
         "M = a pool machine, every node on it restarted; V = one of its nodes, with a DID set up as "
         "quorum; A = a wallet elsewhere whose next 2 tokens have long histories, signing with V's "
         "quorum; B = a DID to pay; count M's IPFS daemons",
         "one IPFS daemon per node of M, every node can receive"),
        ("S2", "V's IPFS daemon dies",
         "kill V's IPFS daemon (only that one); wait up to {}s for Rubix to start it again".format(BACK_WAIT),
         "a new daemon runs and V can receive again (its p2p listener is back)"),
        ("S3", "Pay through V",
         "A pays B 1 RBT, signed by V's quorum",
         "accepted; or, if V cannot be reached, a refusal that says so"),
        ("S4", "Stop V",
         "count M's IPFS daemons; stop V's node; count again; start V's node",
         "only V's daemon is gone"),
        ("S5", "Put M right", "restart every node of M", "every node of M can receive again"),
    ],
}

CASE_INFO = {
    "IPFS-RESTART": (
        "After a node's IPFS restarts on its own, no other node can reach it (its API still answers); "
        "payments that need it are refused with a misleading message; stopping it then kills the "
        "IPFS of every node on the same machine.",
        "The node can be reached again once its IPFS is back; a refusal says the node cannot be "
        "reached; stopping one node leaves its neighbours alone."),
}


def ipfs_restart(ctx, ci):
    case = "IPFS-RESTART"
    run, kit, p = steps.CaseRun(ctx, case, STEPS[case]), kit_mod.Kit(ctx, case), {}

    def setup():
        V = kit.other_host(min_dids=2)
        if not V:
            return False, "no free machine left"
        M = ch.machine(V)
        p.update(V=V, M=M, folder="node{}".format(kit_mod.slot(V)))
        ok, why = kit_mod.restart_machine(M)
        if not ok:
            return False, why
        kit.machines.add(M)
        VQ, why = kit.quorum(need=5, on=V, role="V's quorum")
        if not VQ:
            return False, why
        A, why = kit.holders(1, k=2, avoid_machines={M})
        if not A:
            return False, why
        A = kit.join("A (payer)", A[0])
        B, why = kit.wallet_on(role="B (payee)", avoid_machines={M}, tokens=kit.tokens_of(A))
        if not B:
            return False, why
        ok, why = kit.wire([A], VQ)
        if not ok:
            return False, why
        daemons = kit_mod.ipfs_daemons(M)
        if daemons is None:
            return False, "{}'s processes could not be read over SSH".format(M)
        if p["folder"] not in daemons:
            return False, "no IPFS daemon runs for {}".format(kit_mod.short(V))
        p.update(VQ=VQ, A=A, B=B, daemons=daemons,
                 notes=["M = {}, {} IPFS daemons ({})".format(M, len(daemons), ", ".join(sorted(daemons))),
                        kit_mod.who(VQ, "V's quorum"), kit_mod.who(A, "A"), kit_mod.who(B, "B")])
        return True, ""
    if kit_mod.set_up(run, p, setup).outcome != steps.PASS:
        return kit_mod.verdict(run, kit, "", "")
    V, M, folder, VQ, A, B = (p[k] for k in ("V", "M", "folder", "VQ", "A", "B"))

    def dies(st):
        pid, why = kit_mod.kill_ipfs(V)
        if not pid:
            st.skip(why)
            return
        st.note("killed V's IPFS daemon, pid {}".format(pid))
        t0, new = time.time(), None
        while time.time() - t0 < BACK_WAIT and not new:
            time.sleep(5)
            now = (kit_mod.ipfs_daemons(M) or {}).get(folder)
            new = now if now and now != pid else None
        if not st.check(bool(new), "Rubix did not start V's IPFS again within {}s".format(BACK_WAIT)):
            return
        time.sleep(15)
        p["deaf"] = not kit_mod.listening(V)
        st.data.update(old_pid=pid, new_pid=new, deaf=p["deaf"], api_answers=kit_mod.answers(V))
        st.note("a new IPFS daemon (pid {}) after {:.0f}s; V's API {}".format(
            new, time.time() - t0, "answers" if st.data["api_answers"] else "does NOT answer"))
        said = kit_mod.log_lines(V, "IPFS recovery", last=3)
        if said:
            st.note("V's log: " + said[-1][:200])
        st.check(not p["deaf"], "V's IPFS came back WITHOUT the node's p2p listener - no peer can reach "
                                "V, although its API answers")
    run.step("S2", dies, nodes=[], watch=False, deliver=False)

    def pay(st):
        kit.note_moved(A, B, A["tokens"][:1], "signed by the node whose IPFS restarted")
        ok, msg, _res = ch.send(st, A, B, rbt=1.0, memo=case, timeout=300)
        if not ok and p.get("deaf"):
            reason = ch.node_reason(msg)
            st.check(not any(m in reason for m in ("did not exist", "liquidity", "does not exist")),
                     "the refusal says '{}' - but the quorum's DID exists; its node could not be "
                     "reached".format(reason[:160]))
    run.step("S3", pay, nodes=[A, B], watch=False, must_accept=True)

    def stop_v(st):
        before = kit_mod.ipfs_daemons(M) or {}
        ok, why = kit_mod.stop(V)
        if not ok:
            st.skip("stopping V failed: " + why)
            return
        time.sleep(8)
        after = kit_mod.ipfs_daemons(M) or {}
        kit_mod.start(V)
        lost = sorted(f for f, pid in before.items() if f != folder and after.get(f) != pid)
        st.data.update(before=sorted(before), after=sorted(after), lost=lost)
        st.note("{} IPFS daemons before stopping V, {} after".format(len(before), len(after)))
        st.check(not lost, "stopping V took {} other node(s)' IPFS daemons with it: {}".format(
            len(lost), ", ".join(lost)))
    run.step("S4", stop_v, nodes=[], watch=False, deliver=False)

    def put_right(st):
        ok, why = kit_mod.restart_machine(M)
        st.note("every node of {} restarted{}".format(M, "" if ok else ": " + why))
        rc.quorum_setup(VQ["host"], VQ["did"])
    run.step("S5", put_right, nodes=[], watch=False, deliver=False, required=False)
    return kit_mod.verdict(run, kit, "after its IPFS restarted the node could not be reached, the error "
                                     "misled, or stopping it hit its neighbours",
                           "the node could be reached again and its neighbours were unharmed")


CASES = {"IPFS-RESTART": ipfs_restart}
ORDER = list(CASES)
NEEDS = {"IPFS-RESTART": {"senders": 0, "receivers": 0, "quorums": 0, "exclusive": True, "last": True}}
NO_DELAY_RERUN = set(ORDER)
