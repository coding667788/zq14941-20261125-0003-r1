"""Repair CASE: choose which recheckable observations to stop trusting.

A repair is a subset of the (at most 16) recheckable observations whose
constraints are paused.  A repair is legal when:

1. every remaining component is consistent;
2. every mandatory receipt remains PROVABLE_APPROVED under the remaining
   constraints;
3. every sealed receipt is PROVABLE_NOT_APPROVED under *every* legal
   assignment of the remaining constraints.

Objective order: minimum re-verification cost (sum of per-observation
costs, default 1), then fewest paused items, then lexicographically smallest
ascending id list.  Every candidate is re-solved from scratch; nothing is
inherited from the baseline audit.
"""

from __future__ import annotations

from .audit import ApprovalPredicate
from .model import BuiltModel, Unary, restrict_component
from .solver import Solver


def _recheck_costs(model: BuiltModel) -> dict[str, int]:
    declared = model.case.raw.get("recheck_costs", {}) or {}
    return {oid: int(declared.get(oid, 1)) for oid in model.case.recheckable}


def _component_cache_key(comp, paused: frozenset[str]) -> tuple:
    return (id(comp), paused)


def _evaluate(model: BuiltModel, paused: frozenset[str],
              solver_cache: dict) -> tuple[bool, dict]:
    """Re-solve the whole case with ``paused`` evidence removed."""
    # rebuild (or reuse) each component under this pause set
    rebuilt: list = []
    for cidx, comp in enumerate(model.components):
        key = _component_cache_key(comp,
                                   frozenset(comp.evidence & paused))
        if key not in solver_cache:
            sub = restrict_component(comp, set(comp.evidence & paused))
            solver = Solver(sub)
            solver_cache[key] = (sub, solver, solver.feasible())
        rebuilt.append(solver_cache[key])

    per_receipt: dict[str, str] = {}
    for cidx, (sub, solver, feasible) in enumerate(rebuilt):
        if not feasible:
            # mark every receipt on this component as failed
            for rc in model.case.receipts:
                if model.comp_of[(rc.generation_id, rc.event_id)] == cidx:
                    per_receipt[rc.receipt_id] = "INFEASIBLE_COMPONENT"
            continue
        for rc in model.case.receipts:
            if model.comp_of[(rc.generation_id, rc.event_id)] != cidx:
                continue
            if rc.event_id not in sub.event_var:
                per_receipt[rc.receipt_id] = "EVENT_UNOBSERVED"
                continue
            var = sub.event_var[rc.event_id]
            M = sub.modulus
            predicate = ApprovalPredicate.build(
                rc.generation_id, model, rc.event_id, rc.digest,
                sub.residues[var], M)
            allowed = predicate.allowed_laps()
            some_allowed = any(solver.feasible(Unary(var, lo, hi, ()))
                               for lo, hi in allowed)
            (dlo, dhi), _ = solver.initial_domains()
            outside: list[tuple[int, int]] = []
            cursor = dlo[var]
            for lo, hi in allowed:
                if cursor <= lo - 1:
                    outside.append((cursor, lo - 1))
                cursor = max(cursor, hi + 1)
            if cursor <= dhi[var]:
                outside.append((cursor, dhi[var]))
            some_rejected = any(solver.feasible(Unary(var, lo, hi, ()))
                                for lo, hi in outside)
            if some_allowed and not some_rejected:
                per_receipt[rc.receipt_id] = "PROVABLE_APPROVED"
            elif some_rejected and not some_allowed:
                per_receipt[rc.receipt_id] = "PROVABLE_NOT_APPROVED"
            else:
                per_receipt[rc.receipt_id] = "INSUFFICIENT_EVIDENCE"

    ok = all(v != "INFEASIBLE_COMPONENT" and v != "EVENT_UNOBSERVED"
             for v in per_receipt.values())
    if ok:
        for rc in model.case.receipts:
            v = per_receipt[rc.receipt_id]
            if rc.mandatory and v != "PROVABLE_APPROVED":
                ok = False
                break
            if rc.sealed and v != "PROVABLE_NOT_APPROVED":
                ok = False
                break
    return ok, per_receipt


def run_repair(model: BuiltModel) -> dict:
    ids = list(model.case.recheckable)
    costs = _recheck_costs(model)
    n = len(ids)

    # enumerate masks in exact objective order: cost, popcount, id list
    masks = list(range(1 << n))
    def sort_key(mask: int) -> tuple:
        chosen = [ids[i] for i in range(n) if mask & (1 << i)]
        return (sum(costs[x] for x in chosen), len(chosen), tuple(chosen))
    masks.sort(key=sort_key)

    solver_cache: dict = {}
    for evaluated, mask in enumerate(masks, start=1):
        paused = frozenset(ids[i] for i in range(n) if mask & (1 << i))
        ok, per_receipt = _evaluate(model, paused, solver_cache)
        if ok:
            chosen = sorted(paused)
            return {
                "case_id": model.case.case_id,
                "mode": "repair",
                "feasible": True,
                "paused_observations": chosen,
                "re_verification_cost": sum(costs[x] for x in chosen),
                "paused_count": len(chosen),
                "receipt_status_under_repair": per_receipt,
                "candidates_evaluated": evaluated,
            }

    return {
        "case_id": model.case.case_id,
        "mode": "repair",
        "feasible": False,
        "paused_observations": [],
        "re_verification_cost": None,
        "paused_count": None,
        "receipt_status_under_repair": {},
        "candidates_evaluated": len(masks),
    }
