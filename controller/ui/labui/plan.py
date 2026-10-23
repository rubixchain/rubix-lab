"""plan.py - the catalogue, and the plan for a run before it starts.

Everything a plan says comes from the runner's own code, so what the page shows
is what the run will do:
  - the cases, their order and their units: the case module (master_cases.py)
    and test_runner.build_units
  - whether a unit fits the fleet, or will be skipped: test_runner.fits on the
    pool the poller last read
  - what the faucet can be asked for: test_runner.unit_targets (what
    prepare_unit tops each quorum and participant up to), plus what a case
    draws itself (a module's FAUCET_DRAW)
  - the knobs, their defaults and help: test_runner.build_parser()

The case modules are re-read when a file in master/ changes, so new cases show
up without restarting the UI. If a module cannot be loaded (half-written), the
last good catalogue stays and the error is shown.

Limits from the lab's rules are warnings here, never blocks: a worst-case
faucet draw of 10,000 RBT or more, a value above 5,000. The run's own checks
stay as they are.

A custom run (the Run builder's second tab) is planned the same way from
custom/custom_cases.py: its check() normalises the scenarios and finds what is
wrong with them, its budget() works out each scenario's operations, quorum
funding and faucet draw, and configure() gives the runner's build_units and
fits the units to place on the pool. Each plan loads its own copy of the
module, so two pages planning at once never share a spec.
"""

import argparse
import csv
import os
import shlex
import sys
import threading

import rubix_client as rc
import run_events
import test_runner as tr

from labui import MASTER

CASE_LIMIT_RBT = 10000      # no case may need this much (the faucet budget)
VALUE_LIMIT_RBT = 5000      # value and wallet ladders stop here

# Checks on the knobs, beyond their type. "max" refuses, "warn_max" warns.
KNOB_RULES = {
    "quorum_floor": {"min": 0, "warn_max": VALUE_LIMIT_RBT},
    "ladder_stop": {"min": 0, "max": 100},
    "ladder_settle": {"min": 0, "warn_max": 600},
    "whole_wallet": {"min": 0, "warn_max": VALUE_LIMIT_RBT},
    "parts_target": {"min": 0, "warn_max": 5000},
    "asset_hops": {"min": 2, "warn_max": 50},
    "scale": {"above": 0, "warn_max": 10},
    "fleet_total": {"above": 0},
}
# The UI turns these on by default (the terminal default is off): a run's
# report then names the build it ran on, and limits can be compared by release.
UI_DEFAULTS = {"collect_versions": True}

CUSTOM = "custom"               # custom/custom_cases.py
SPEC_ARG = "{spec}"             # in a custom plan's argv: where the spec file goes


def _sh(argv):
    return " ".join(shlex.quote(str(a)) for a in argv)


class Catalogue(object):
    def __init__(self, name="master"):
        self.name = name
        self.lock = threading.RLock()
        self.module = None
        self.rows = {}
        self.error = ""
        self._stamp = None

    # -- loading -----------------------------------------------------------------

    def _files_stamp(self):
        stamp = []
        for f in sorted(os.listdir(MASTER)):
            if f.endswith((".py", ".csv")):
                try:
                    stamp.append((f, os.path.getmtime(os.path.join(MASTER, f))))
                except OSError:
                    pass
        return tuple(stamp)

    def load(self):
        with self.lock:
            stamp = self._files_stamp()
            if stamp == self._stamp and self.module is not None:
                return self.module
            # Re-import the case modules from disk, not from the import cache.
            master = os.path.abspath(MASTER)
            for name, mod in list(sys.modules.items()):
                f = getattr(mod, "__file__", None) or ""
                if f and os.path.dirname(os.path.abspath(f)) == master:
                    del sys.modules[name]
            try:
                module = tr.load_case_module(self.name)
                rows = {}
                with open(tr.CATALOGUE_PATH, encoding="utf-8") as fh:
                    for row in csv.DictReader(fh):
                        tid = (row.get("Test ID") or "").strip()
                        if tid:
                            rows[tid] = row
                self.module, self.rows, self.error = module, rows, ""
            except BaseException as e:      # SystemExit included: the loader exits on errors
                self.error = "the case modules could not be loaded ({}: {}) - showing the last " \
                             "good catalogue".format(type(e).__name__, e)
            self._stamp = stamp
            return self.module

    def _modules(self):
        return list(getattr(self.module, "CASE_MODULES", None) or [self.module])

    def faucet_draws(self):
        draws = {}
        for m in self._modules():
            draws.update(getattr(m, "FAUCET_DRAW", None) or {})
        return draws

    # -- knobs ---------------------------------------------------------------------

    def knob_specs(self):
        """[{name, flag, type, default, help}] for the scale knobs, from the runner's parser."""
        out = []
        for a in tr.build_parser()._actions:
            if a.dest not in run_events.KNOBS:
                continue
            kind = ("bool" if isinstance(a, argparse._StoreTrueAction) else
                    "int" if a.type is int else "float" if a.type is float else "str")
            out.append({"name": a.dest, "flag": a.option_strings[0], "type": kind,
                        "default": a.default, "ui_default": UI_DEFAULTS.get(a.dest, a.default),
                        "help": (a.help or "").replace("%(default)s", str(a.default)),
                        "rules": KNOB_RULES.get(a.dest, {})})
        return out

    def _knobs(self, given):
        """(values, argv, errors, warnings) for the knobs the page sent."""
        values, argv, errors, warnings = {}, [], [], []
        for k in self.knob_specs():
            name, raw = k["name"], given.get(k["name"], k["ui_default"])
            label = k["flag"]
            try:
                if k["type"] == "bool":
                    v = bool(raw)
                elif k["type"] == "int":
                    v = int(raw)
                elif k["type"] == "float":
                    v = float(raw)
                else:
                    v = str(raw if raw is not None else "").strip()
            except (TypeError, ValueError):
                errors.append("{}: {!r} is not a {}".format(label, raw, k["type"]))
                continue
            rules = k["rules"]
            if "min" in rules and v < rules["min"]:
                errors.append("{} must be at least {}".format(label, rules["min"]))
            if "above" in rules and v <= rules["above"]:
                errors.append("{} must be above {}".format(label, rules["above"]))
            if "max" in rules and v > rules["max"]:
                errors.append("{} must be at most {}".format(label, rules["max"]))
            if "warn_max" in rules and v > rules["warn_max"]:
                warnings.append("{} {} is above the lab's usual ceiling of {}".format(
                    label, v, rules["warn_max"]))
            if name == "quorum_ladder":
                try:
                    rungs = [int(x) for x in v.split(",") if x.strip()]
                except ValueError:
                    errors.append("{}: whole numbers separated by commas, e.g. 10,20,50".format(label))
                    rungs = []
                if v and not rungs:
                    errors.append("{}: at least one rung".format(label))
                if any(r < 1 for r in rungs):
                    errors.append("{}: every rung must be 1 or more".format(label))
                if rungs != sorted(set(rungs)):
                    warnings.append("{}: rungs are climbed in rising order, duplicates once - "
                                    "{} becomes {}".format(label, v, ",".join(
                                        str(r) for r in sorted(set(rungs)))))
            values[name] = v
            if v != k["default"]:
                if k["type"] == "bool":
                    if v:
                        argv.append(label)
                else:
                    argv += [label, str(v)]
        return values, argv, errors, warnings

    # -- what the page shows -------------------------------------------------------

    def info(self):
        module = self.load()
        if module is None:
            return {"module": self.name, "error": self.error or "no case module", "cases": [],
                    "units": [], "knobs": self.knob_specs()}
        unit_of, units = {}, []
        try:
            for u in tr.build_units(module, list(module.ORDER)):
                for c in u.cases:
                    unit_of[c] = u.name
                units.append({"name": u.name, "cases": list(u.cases), "exclusive": u.exclusive,
                              "first": bool(u.spec["first"]), "last": bool(u.spec["last"]),
                              "spec": dict((k, v) for k, v in u.spec.items())})
        except BaseException as e:
            return {"module": self.name, "error": "{}: {}".format(type(e).__name__, e),
                    "cases": [], "units": [], "knobs": self.knob_specs()}
        file_of = {}
        for m in self._modules():
            for tid in getattr(m, "ORDER", []):
                file_of[tid] = os.path.basename(getattr(m, "__file__", "") or "")
        cases = []
        for tid in module.ORDER:
            row = self.rows.get(tid, {})
            cases.append({"id": tid, "unit": unit_of.get(tid), "module": file_of.get(tid, ""),
                          "asset": row.get("Asset", ""), "group": row.get("Op Group", ""),
                          "case": row.get("Test Case", ""), "setup": row.get("Setup Method", ""),
                          "quorum_setup": row.get("Quorum Setup", ""),
                          "expected": row.get("Expected Result", ""),
                          "checks": row.get("Also Check In Same Run", ""),
                          "priority": row.get("Priority", ""), "code_ref": row.get("Code Ref", ""),
                          "in_catalogue": bool(row),
                          "no_rerun": tid in set(getattr(module, "NO_DELAY_RERUN", ()) or ())})
        return {"module": self.name, "error": self.error, "cases": cases, "units": units,
                "knobs": self.knob_specs()}

    @staticmethod
    def _fleet_checks(pool, faucet, errors, warnings):
        """(pool, machines, faucet free RBT) - and what stops a run, from the
        poller's last reading."""
        if pool is None:
            errors.append("the fleet has not been read yet - wait for the Fleet page to fill in")
            pool = []
        elif not pool:
            errors.append("no usable pool node: none is up with a DID")
        machines = len(set(n["machine"] for n in pool))
        info = faucet or {}
        if faucet is None:
            warnings.append("the faucet has not been read yet; it is checked again at Start")
        elif not info.get("ok"):
            errors.append("faucet not ready - {}".format(info.get("note")))
        faucet_free = ((info.get("faucet") or {}).get("balance")) if info else None
        return pool, machines, faucet_free

    def plan(self, req, pool, faucet):
        """What a run would do, and whether it may start. `pool` is the poller's
        usable pool nodes (None before the fleet has been read), `faucet` its
        faucet reading. req["mode"] "custom" plans a custom run."""
        if req.get("mode") == CUSTOM:
            return self.custom_plan(req, pool, faucet)
        errors, warnings = [], []
        module = self.load()
        if self.error:
            (errors if module is None else warnings).append(self.error)
        if module is None:
            return {"ok": False, "errors": errors, "warnings": warnings, "units": []}

        selected = [str(t) for t in (req.get("cases") or [])]
        unknown = sorted(set(selected) - set(module.CASES))
        if unknown:
            errors.append("not in the catalogue: " + ", ".join(unknown))
        order = [t for t in module.ORDER if t in set(selected)] if selected else list(module.ORDER)
        if not order:
            errors.append("pick at least one case")
        knobs, knob_argv, kerr, kwarn = self._knobs(req.get("knobs") or {})
        errors += kerr
        warnings += kwarn

        pool, machines, faucet_free = self._fleet_checks(pool, faucet, errors, warnings)

        units_out, total = [], 0.0
        if order and not unknown:
            try:
                units = tr.build_units(module, order)
                all_units = dict((u.name, u) for u in tr.build_units(module, list(module.ORDER)))
            except BaseException as e:      # build_units exits on a module error
                errors.append("{}: {}".format(type(e).__name__, e))
                units, all_units = [], {}
            args = argparse.Namespace(port=rc.DEFAULT_PORT, **knobs)
            draws = self.faucet_draws()
            for u in units:
                s = u.spec
                q, r = int(s["quorums"] or 0), int(s["receivers"] or 0)
                senders = max(0, len(pool) - q - r) if u.wants_all else int(s["senders"] or 0)
                people = senders + r
                floor, want = tr.unit_targets(s, args.quorum_floor)
                extra = 0.0
                for c in u.cases:
                    if c in draws:
                        try:
                            extra += float(draws[c](args))
                        except Exception:
                            pass
                draw = q * (floor or 0) + people * (want or 0) + extra
                total += draw
                if u.needs_nodes and pool:
                    now, ever, why = tr.fits(u, pool, pool)
                else:
                    now, ever, why = bool(pool) or not u.needs_nodes, True, ""
                if not ever:
                    warnings.append("{} will be SKIPPED by the runner: {}".format(u.name, why))
                if not u.needs_nodes:
                    nodes = 0
                elif u.wants_all:
                    nodes = senders + q + r
                else:
                    nodes = u.node_count()
                full = all_units.get(u.name)
                if full is not None and len(full.cases) > len(u.cases):
                    warnings.append("{}: only {} of its cases {} picked. They share participants "
                                    "and run in order, so a later one may need what an earlier "
                                    "one builds".format(u.name, ", ".join(u.cases),
                                                        ", ".join(full.cases)))
                if draw >= CASE_LIMIT_RBT:
                    warnings.append("{} can draw up to {:,.0f} RBT from the faucet - the lab keeps "
                                    "every case under {:,}".format(u.name, draw, CASE_LIMIT_RBT))
                units_out.append({
                    "name": u.name, "cases": list(u.cases), "exclusive": u.exclusive,
                    "first": bool(s["first"]), "last": bool(s["last"]),
                    "same_node": bool(s["same_node"]),
                    "quorums": q, "senders": senders, "senders_all": u.wants_all,
                    "receivers": r, "nodes": nodes,
                    "fits": bool(now and ever), "skip": not ever, "why": why,
                    "quorum_target": floor if q else 0, "participant_target": want,
                    "extra": extra, "draw": draw,
                })
        if total >= CASE_LIMIT_RBT:
            warnings.append("in the worst case (every participant empty) this run draws {:,.0f} RBT "
                            "from the faucet. It draws only shortfalls - DIDs keep what they hold "
                            "- so the real draw is usually far less".format(total))
        if faucet_free is not None and total > faucet_free:
            warnings.append("the faucet holds {:,.3f} RBT free, less than the worst-case draw of "
                            "{:,.0f}".format(faucet_free, total))

        argv = ["--cases", self.name]
        if selected and order != list(module.ORDER):
            argv += ["--only", ",".join(order)]
        argv += knob_argv
        return {"ok": not errors, "errors": errors, "warnings": warnings, "units": units_out,
                "order": order, "argv": argv, "knobs": knobs,
                "command": "python3 test_runner.py " + _sh(argv),
                "pool": {"nodes": len(pool), "machines": machines,
                         "dids": sum(len(n["dids"]) for n in pool)},
                "draw": {"worst": total, "faucet_free": faucet_free}}

    # -- custom runs -----------------------------------------------------------------

    def custom_module(self):
        """A fresh copy of custom/custom_cases.py: each plan configures its own."""
        return tr.load_case_module(CUSTOM)

    def custom_form(self):
        """What the Custom run form offers (custom_cases.form())."""
        try:
            return self.custom_module().form()
        except BaseException as e:
            return {"error": "custom/custom_cases.py could not be loaded ({}: {})".format(
                type(e).__name__, e)}

    def custom_plan(self, req, pool, faucet):
        """What a custom run would do: each scenario's participants, operations,
        quorum funding and faucet draw, and whether it fits the fleet."""
        errors, warnings = [], []
        try:
            module = self.custom_module()
        except BaseException as e:
            return {"ok": False, "mode": CUSTOM, "units": [], "warnings": [],
                    "errors": ["custom/custom_cases.py could not be loaded ({}: {})".format(
                        type(e).__name__, e)]}
        norm, serr, swarn = module.check(req.get("spec") or {})
        errors += serr
        warnings += swarn
        collect = bool(req.get("collect_versions", True))
        pool, machines, faucet_free = self._fleet_checks(pool, faucet, errors, warnings)

        units_out, total, order = [], 0.0, []
        if norm and not serr:
            module.configure(norm)
            order = list(module.ORDER)
            by_id = dict((sc["id"], sc) for sc in norm["scenarios"])
            for u in tr.build_units(module, order):
                sc = by_id[u.cases[0]]
                b = module.budget(sc)
                if pool:
                    now, ever, why = tr.fits(u, pool, pool)
                else:
                    now, ever, why = False, True, ""
                if not ever:
                    errors.append("{} cannot be placed: {}".format(u.name, why))
                # FT mint makes no transfer, so the runner runs it once either way.
                runs = 2 if norm["rerun"] == "twice" and \
                    "{}:{}".format(sc["asset"], sc["operation"]) != "FT:mint" else 1
                draw = b["draw"] * runs
                total += draw
                if draw >= CASE_LIMIT_RBT:
                    warnings.append("{} can draw up to {:,.0f} RBT from the faucet - the lab keeps "
                                    "every case under {:,}".format(u.name, draw, CASE_LIMIT_RBT))
                units_out.append({
                    "name": u.name, "cases": list(u.cases), "summary": sc["summary"],
                    "exclusive": u.exclusive, "same_node": bool(sc["same_node"]),
                    "quorums": sc["quorums"], "senders": sc["senders"],
                    "receivers": sc["receivers"], "nodes": u.node_count(),
                    "fits": bool(now and ever), "skip": not ever, "why": why,
                    "ops": b["ops"] * runs, "at_once": b["at_once"], "value": b["value"] * runs,
                    "quorum_target": b["quorum_fund"], "quorum_need": b["quorum_need"],
                    "sender_need": b["sender_need"], "receiver_need": b["receiver_need"],
                    "participant_target": b["sender_need"], "extra": 0, "draw": draw, "runs": runs,
                })
            busy = sum(u["nodes"] or 0 for u in units_out)
            if pool and len(units_out) > 1 and busy > len(pool):
                warnings.append("the scenarios need {} nodes in all and {} are usable - they run "
                                "one after another as nodes free up".format(busy, len(pool)))
        if total >= CASE_LIMIT_RBT:
            warnings.append("in the worst case (every participant empty) this run draws {:,.0f} RBT "
                            "from the faucet. It draws only shortfalls - DIDs keep what they hold "
                            "- so the real draw is usually far less".format(total))
        if faucet_free is not None and total > faucet_free:
            warnings.append("the faucet holds {:,.3f} RBT free, less than the worst-case draw of "
                            "{:,.0f}".format(faucet_free, total))
        argv = ["--cases", CUSTOM, "--spec", SPEC_ARG, "--report-name", CUSTOM]
        if collect:
            argv.append("--collect-versions")
        shown = [a if a != SPEC_ARG else "custom-spec.json" for a in argv]
        return {"ok": not errors, "mode": CUSTOM, "errors": errors, "warnings": warnings,
                "units": units_out, "order": order, "argv": argv, "spec": norm,
                "knobs": {"collect_versions": collect},
                "command": "python3 test_runner.py " + _sh(shown),
                "pool": {"nodes": len(pool), "machines": machines,
                         "dids": sum(len(n["dids"]) for n in pool)},
                "draw": {"worst": total, "faucet_free": faucet_free}}
