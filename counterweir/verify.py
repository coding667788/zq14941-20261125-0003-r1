"""Verify CASE: independently re-solve and check a claimed audit/repair result.

The verifier never trusts the supplied result.  It rebuilds the model, solves
every constraint again from scratch, and compares each claim.  The three
known failure modes are called out explicitly:

* one-sided witnesses -- an INSUFFICIENT_EVIDENCE claim whose allow or reject
  assignment is missing or not itself a satisfying full assignment;
* same-cost later plans -- a repair claim whose objective tuple is beaten by
  an earlier candidate under cost / count / lexicographic id order;
* residue conflation -- a claim that implicitly merges distinct events with
  the same residue, detected by checking every witness assignment against the
  distinctness constraints and the one-event-one-sequence rule.
"""

from __future__ import annotations

from .audit import run_audit
from .model import BuiltModel
from .repair import _evaluate, _recheck_costs


def _check_witness(model: BuiltModel, assignment: dict, generation_id: str
                   ) -> list[str]:
    """Validate a full or partial sequence assignment against every constraint."""
    errors: list[str] = []
    gen = model.case.generations[generation_id]
    M = gen.modulus
    # residue agreement
    obs_residue: dict[str, int] = {}
    for o in gen.observations:
        obs_residue.setdefault(o.event_id, o.residue)
    for ev, seq in assignment.items():
        if ev in obs_residue and seq % M != obs_residue[ev]:
            errors.append(
                f"witness assigns {ev}={seq} but residue must be "
                f"{obs_residue[ev]} (residue conflation)")
        if seq < 0:
            errors.append(f"witness assigns negative sequence to {ev}")
    # distinctness: equal-residue distinct events must differ
    seen_seq: dict[int, str] = {}
    for ev, seq in assignment.items():
        r = obs_residue.get(ev, seq % M)
        if seq % M != r:
            continue
        if seq in seen_seq and seen_seq[seq] != ev:
            errors.append(
                f"witness merges distinct events {seen_seq[seq]} and {ev} "
                f"at sequence {seq} (same residue is not the same event)")
        seen_seq[seq] = ev
    # anchor agreement
    for a in gen.anchors:
        if a.event_id in assignment and assignment[a.event_id] != a.sequence:
            errors.append(
                f"witness contradicts anchor {a.anchor_id}: "
                f"{a.event_id}={assignment[a.event_id]} != {a.sequence}")
    # source ordering gaps
    per_source: dict[str, list] = {}
    for o in gen.observations:
        per_source.setdefault(o.source_id, []).append(o)
    for olist in per_source.values():
        olist.sort(key=lambda o: o.ordinal)
        for prev, cur in zip(olist, olist[1:]):
            if prev.event_id in assignment and cur.event_id in assignment:
                d = assignment[cur.event_id] - assignment[prev.event_id]
                if not (cur.gap_min <= d <= cur.gap_max):
                    errors.append(
                        f"witness violates source {cur.source_id} gap at "
                        f"{cur.obs_id}: delta {d} outside "
                        f"[{cur.gap_min}, {cur.gap_max}]")
    return errors


def run_verify(model: BuiltModel, claimed: dict) -> dict:
    errors: list[dict] = []
    mode = claimed.get("mode", "audit")

    actual_audit = run_audit(model)
    actual_by_id = {r["receipt_id"]: r for r in actual_audit["receipts"]}

    if mode == "audit":
        for claim in claimed.get("receipts", []):
            rid = claim.get("receipt_id")
            actual = actual_by_id.get(rid)
            if actual is None:
                errors.append({"receipt_id": rid,
                               "error": "unknown receipt in claimed result"})
                continue
            if claim.get("verdict") != actual["verdict"]:
                errors.append({
                    "receipt_id": rid,
                    "error": f"verdict mismatch: claimed {claim.get('verdict')}"
                             f", re-solved {actual['verdict']}"})
                continue
            for key in ("earliest_possible_sequence",
                        "latest_possible_sequence"):
                if key in claim and claim[key] != actual[key]:
                    errors.append({
                        "receipt_id": rid,
                        "error": f"{key} mismatch: claimed {claim[key]}, "
                                 f"re-solved {actual[key]}"})
            if actual["verdict"] == "INSUFFICIENT_EVIDENCE":
                for side in ("allow_assignment", "reject_assignment"):
                    assignment = claim.get(side)
                    if not assignment:
                        errors.append({"receipt_id": rid,
                                       "error": f"one-sided witness: {side} missing"})
                        continue
                    for msg in _check_witness(
                            model, assignment, claim["generation_id"]):
                        errors.append({"receipt_id": rid, "error": msg})
                    # the side must satisfy its claimed approval posture
                    rc = next(r for r in model.case.receipts
                              if r.receipt_id == rid)
                    if not _posture_matches(model, rc, assignment,
                                            approved=side == "allow_assignment"):
                        errors.append({
                            "receipt_id": rid,
                            "error": f"{side} does not realize its claimed "
                                     "approval posture"})
        # claimed contradictions must reproduce the minimal evidence set
        actual_mus = {
            (c["generation_id"], tuple(c["events"])): c["minimal_evidence"]
            for c in actual_audit["contradictions"]}
        for cc in claimed.get("contradictions", []):
            key = (cc["generation_id"], tuple(cc["events"]))
            if actual_mus.get(key) != cc.get("minimal_evidence"):
                errors.append({
                    "generation_id": cc["generation_id"],
                    "error": "minimal contradictory evidence set mismatch"})

    elif mode == "repair":
        ids = list(model.case.recheckable)
        costs = _recheck_costs(model)
        claimed_paused = sorted(claimed.get("paused_observations", []))
        unknown = [x for x in claimed_paused if x not in ids]
        if unknown:
            errors.append({"error": f"unknown paused observations: {unknown}"})
        else:
            paused = frozenset(claimed_paused)
            ok, status = _evaluate(model, paused, {})
            if not ok:
                errors.append({
                    "error": "claimed repair is not legal under re-solve",
                    "status": status})
            # objective minimality: no earlier candidate may be legal
            def key_of(chosen: list[str]) -> tuple:
                return (sum(costs[x] for x in chosen), len(chosen),
                        tuple(chosen))
            claimed_key = key_of(claimed_paused)
            n = len(ids)
            for mask in range(1 << n):
                cand = sorted(ids[i] for i in range(n) if mask & (1 << i))
                if key_of(cand) >= claimed_key:
                    continue
                cand_ok, _ = _evaluate(model, frozenset(cand), {})
                if cand_ok:
                    errors.append({
                        "error": "same-cost later plan: a strictly better "
                                 f"repair exists {cand} vs {claimed_paused}"})
                    break
    else:
        errors.append({"error": f"unknown mode {mode}"})

    return {
        "case_id": model.case.case_id,
        "mode": "verify",
        "claimed_mode": mode,
        "accepted": not errors,
        "errors": errors,
        "re_solved_receipts": actual_audit["receipts"],
    }


def _posture_matches(model: BuiltModel, receipt, assignment: dict,
                     approved: bool) -> bool:
    from .audit import ApprovalPredicate
    gen = model.case.generations[receipt.generation_id]
    M = gen.modulus
    seq = assignment[receipt.event_id]
    predicate = ApprovalPredicate.build(
        receipt.generation_id, model, receipt.event_id, receipt.digest,
        seq % M, M)
    k = (seq - seq % M) // M
    inside_allowed = any(lo <= k <= hi for lo, hi in predicate.allowed_laps())
    return inside_allowed if approved else not inside_allowed
