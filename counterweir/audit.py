"""Audit CASE: per-receipt verdicts over all legal sequence assignments."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations

from .model import (BuiltModel, Component, Receipt, Unary,
                    restrict_component)
from .solver import Solver

# Replayable MUS enumeration is capped: ambiguous components hold at most 16
# events, so their backing evidence set stays small in practice.
MUS_EVIDENCE_CAP = 24

FULL_ASSIGNMENT_LIMIT = 64


# --------------------------------------------------------------------------- #
# Approval predicate: actual-sequence intervals -> lap intervals
# --------------------------------------------------------------------------- #

def _lap_intervals(lo: int, hi: int, residue: int, M: int
                   ) -> list[tuple[int, int]]:
    """Integer k with ``lo <= residue + M*k <= hi``."""
    if hi < residue:
        return []
    k_lo = -((-(lo - residue)) // M)       # ceil((lo - residue)/M)
    k_hi = (hi - residue) // M
    if k_lo > k_hi:
        return []
    return [(max(0, k_lo), k_hi)]


def _merge(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for lo, hi in sorted(intervals):
        if out and lo <= out[-1][1] + 1:
            out[-1] = (out[-1][0], max(out[-1][1], hi))
        else:
            out.append((lo, hi))
    return out


@dataclass
class ApprovalPredicate:
    """Lap intervals where the event is approved / revoked for one digest."""
    approved: list[tuple[int, int]]
    revoked: list[tuple[int, int]]

    @classmethod
    def build(cls, gen_id: str, model: BuiltModel, event: str, digest: str,
              residue: int, M: int) -> "ApprovalPredicate":
        gen = model.case.generations[gen_id]
        appr: list[tuple[int, int]] = []
        for iv in gen.approvals:
            if iv.digest == digest:
                appr += _lap_intervals(iv.lo, iv.hi, residue, M)
        rev: list[tuple[int, int]] = []
        for iv in gen.revocations:
            rev += _lap_intervals(iv.lo, iv.hi, residue, M)
        return cls(_merge(appr), _merge(rev))

    def allowed_laps(self) -> list[tuple[int, int]]:
        """Approved laps with every revoked point removed (still interval sets)."""
        out = list(self.approved)
        for rlo, rhi in self.revoked:
            nxt: list[tuple[int, int]] = []
            for lo, hi in out:
                if rhi < lo or rlo > hi:
                    nxt.append((lo, hi))
                    continue
                if lo <= rlo - 1:
                    nxt.append((lo, rlo - 1))
                if rhi + 1 <= hi:
                    nxt.append((rhi + 1, hi))
            out = nxt
        return _merge(out)


def _intersects(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return not (a[1] < b[0] or b[1] < a[0])


# --------------------------------------------------------------------------- #
# Minimal unsatisfiable evidence subset
# --------------------------------------------------------------------------- #

def find_contradiction(comp: Component) -> list[str]:
    """Smallest replayable evidence subset that is itself inconsistent.

    Tie-break: fewer evidence items, then lexicographically ascending id list.
    """
    evidence = sorted(comp.evidence)
    if len(evidence) > MUS_EVIDENCE_CAP:
        raise RuntimeError(
            f"component evidence set ({len(evidence)}) exceeds MUS cap; "
            "the 16-event limit should prevent this")
    for size in range(1, len(evidence) + 1):
        for subset in combinations(evidence, size):
            sub = restrict_component(comp, set(comp.evidence) - set(subset))
            if not Solver(sub).feasible():
                return list(subset)
    raise RuntimeError("component reported infeasible but no MUS found")


# --------------------------------------------------------------------------- #
# Component audit
# --------------------------------------------------------------------------- #

@dataclass
class ComponentResult:
    component: Component
    feasible: bool
    solver: Solver | None
    mus: list[str] | None
    fixed_assignment: dict[int, int] | None = None


def audit_components(model: BuiltModel) -> list[ComponentResult]:
    results: list[ComponentResult] = []
    for comp in model.components:
        solver = Solver(comp)
        if not solver.feasible():
            results.append(ComponentResult(comp, False, None,
                                           find_contradiction(comp)))
            continue
        # one propagation captures the unique assignment of a deterministic
        # (e.g. long aligned) component, so per-receipt work stays O(1)
        fixed = None
        if comp.deterministic:
            (lo, hi), excluded = solver.initial_domains()
            solver.propagate([lo, hi], excluded,
                             list(range(len(comp.var_event))))
            fixed = {v: lo[v] for v in range(len(comp.var_event))}
        results.append(ComponentResult(comp, True, solver, None, fixed))
    return results


def _full_assignment(solver: Solver, comp: Component,
                     assignment: dict[int, int]) -> dict[str, int]:
    if len(comp.var_event) <= FULL_ASSIGNMENT_LIMIT:
        return solver.to_sequences(assignment)
    # Oversized deterministic component: every value is uniquely forced by
    # bounds propagation, so a full replay is obtained by re-running that
    # propagation.  Emit anchor-pinned values plus a deterministic marker
    # rather than a 200k-entry map.
    out = {comp.var_event[v]: comp.residues[v] + comp.modulus * assignment[v]
           for v in assignment if comp.pinned[v]}
    out["_deterministic_component_note"] = (
        f"{len(comp.var_event)} events uniquely fixed by propagation; "
        "replay via counterweir bounds propagation on the same constraints")
    return out


def _receipt_verdict(model: BuiltModel, cr: ComponentResult,
                     receipt: Receipt) -> dict:
    comp = cr.component
    M = comp.modulus
    var = comp.event_var[receipt.event_id]
    residue = comp.residues[var]
    predicate = ApprovalPredicate.build(
        receipt.generation_id, model, receipt.event_id, receipt.digest,
        residue, M)
    allowed = predicate.allowed_laps()
    solver: Solver = cr.solver

    # Deterministic fast path: propagation already proved every variable has
    # a single feasible value, so no receipt-level search is required.  This
    # keeps a 200k-event aligned chain at one propagation plus O(receipts).
    if cr.fixed_assignment is not None and var in cr.fixed_assignment:
        k = cr.fixed_assignment[var]
        seq = residue + M * k
        approved = any(lo <= k <= hi for lo, hi in allowed)
        witness = _full_assignment(solver, comp, cr.fixed_assignment)
        return {
            "receipt_id": receipt.receipt_id,
            "generation_id": receipt.generation_id,
            "event_id": receipt.event_id,
            "digest": receipt.digest,
            "earliest_possible_sequence": seq,
            "latest_possible_sequence": seq,
            "earliest_witness_assignment": witness,
            "latest_witness_assignment": witness,
            "verdict": "PROVABLE_APPROVED" if approved
                       else "PROVABLE_NOT_APPROVED"}

    def feasible_inside(intervals: list[tuple[int, int]]) -> bool:
        return any(solver.feasible(Unary(var, lo, hi, ()))
                   for lo, hi in intervals)

    some_allowed = feasible_inside(allowed)
    # a rejecting assignment exists iff some feasible lap lies outside `allowed`
    (dlo, dhi), _ = solver.initial_domains()
    outside: list[tuple[int, int]] = []
    cursor = dlo[var]
    for lo, hi in allowed:
        if cursor <= lo - 1:
            outside.append((cursor, lo - 1))
        cursor = max(cursor, hi + 1)
    if cursor <= dhi[var]:
        outside.append((cursor, dhi[var]))
    some_rejected = feasible_inside(outside)

    min_k, min_assign = solver.extremum(var, minimize=True)
    max_k, max_assign = solver.extremum(var, minimize=False)
    base = {
        "receipt_id": receipt.receipt_id,
        "generation_id": receipt.generation_id,
        "event_id": receipt.event_id,
        "digest": receipt.digest,
        "earliest_possible_sequence": residue + M * min_k,
        "latest_possible_sequence": residue + M * max_k,
        "earliest_witness_assignment": _full_assignment(
            solver, comp, min_assign),
        "latest_witness_assignment": _full_assignment(
            solver, comp, max_assign),
    }

    if some_allowed and not some_rejected:
        base["verdict"] = "PROVABLE_APPROVED"
        return base
    if some_rejected and not some_allowed:
        base["verdict"] = "PROVABLE_NOT_APPROVED"
        return base
    if not some_allowed and not some_rejected:
        # defensive: feasibility was already established, cannot happen
        base["verdict"] = "CONTRADICTORY_EVIDENCE"
        return base

    # INSUFFICIENT_EVIDENCE: one full replayable assignment on each side
    allow_witness = None
    for lo, hi in allowed:
        w = solver.witness(Unary(var, lo, hi, ()))
        if w is not None:
            allow_witness = solver.to_sequences(w)
            break
    reject_witness = None
    for lo, hi in outside:
        w = solver.witness(Unary(var, lo, hi, ()))
        if w is not None:
            reject_witness = solver.to_sequences(w)
            break
    base["verdict"] = "INSUFFICIENT_EVIDENCE"
    base["allow_assignment"] = allow_witness
    base["reject_assignment"] = reject_witness
    base["approved_lap_intervals"] = allowed
    return base


def run_audit(model: BuiltModel) -> dict:
    component_results = audit_components(model)
    cr_by_index = {id(cr.component): cr for cr in component_results}

    contradictions: list[dict] = []
    for cr in component_results:
        if not cr.feasible:
            contradictions.append({
                "generation_id": cr.component.generation_id,
                "events": list(cr.component.var_event),
                "minimal_evidence": cr.mus,
            })

    receipts_out: list[dict] = []
    for receipt in model.case.receipts:
        cidx = model.comp_of[(receipt.generation_id, receipt.event_id)]
        cr = component_results[cidx]
        if not cr.feasible:
            receipts_out.append({
                "receipt_id": receipt.receipt_id,
                "generation_id": receipt.generation_id,
                "event_id": receipt.event_id,
                "digest": receipt.digest,
                "verdict": "CONTRADICTORY_EVIDENCE",
                "minimal_evidence": cr.mus,
            })
        else:
            receipts_out.append(_receipt_verdict(model, cr, receipt))

    return {
        "case_id": model.case.case_id,
        "mode": "audit",
        "contradictions": contradictions,
        "receipts": receipts_out,
    }
