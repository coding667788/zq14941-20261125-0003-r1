"""Exhaustive cross-checks against a brute-force enumeration oracle.

Every ambiguous case here is built with small lap domains, so
:func:`counterweir.solver.brute_force_solutions` can list *every* legal full
assignment.  The production solver must agree on feasibility, the exact
feasible sequence set per event, extremal values, and every receipt verdict.
The brute force also merges nothing: equal residues stay distinct events.
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from counterweir.audit import (ApprovalPredicate, run_audit)
from counterweir.model import (ModelError, Unary, build_components,
                               load_case, parse_case, restrict_component)
from counterweir.repair import run_repair
from counterweir.solver import Solver, brute_force_solutions
from counterweir.verify import run_verify

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "fixtures")


def _audit_brute(receipt, model, comp, solutions):
    """Reference verdict computed directly from enumerated assignments."""
    M = comp.modulus
    var = comp.event_var[receipt.event_id]
    residue = comp.residues[var]
    predicate = ApprovalPredicate.build(
        receipt.generation_id, model, receipt.event_id, receipt.digest,
        residue, M)
    allowed = predicate.allowed_laps()
    statuses = []
    for sol in solutions:
        seq = sol[receipt.event_id]
        k = (seq - residue) // M
        ok = any(lo <= k <= hi for lo, hi in allowed)
        statuses.append(ok)
    if all(statuses):
        return "PROVABLE_APPROVED"
    if not any(statuses):
        return "PROVABLE_NOT_APPROVED"
    return "INSUFFICIENT_EVIDENCE"


class Fixture1Tests(unittest.TestCase):
    def setUp(self):
        self.case = load_case(os.path.join(FIX,
                                           "fixture1_two_lap_two_source.json"))
        self.model = build_components(self.case)
        self.comp = self.model.components[0]
        self.brute = brute_force_solutions(self.comp)
        self.solver = Solver(self.comp)

    def test_feasibility_and_full_solution_set(self):
        self.assertTrue(self.solver.feasible())
        # hand-enumerated feasible assignments (S2 eliminates E1=10 only;
        # E2=11 survives because S1's gap [1,5] allows delta 5 from E1=6)
        self.assertEqual(
            sorted((s["E1"], s["E2"], s["E3"]) for s in self.brute),
            sorted([(2, 3, 10), (2, 7, 10), (6, 7, 10), (6, 11, 10)]))

    def test_extrema_match_brute_force(self):
        for ev in ("E1", "E2", "E3"):
            var = self.comp.event_var[ev]
            seqs = sorted(s[ev] for s in self.brute)
            kmin, amin = self.solver.extremum(var, True)
            kmax, amax = self.solver.extremum(var, False)
            self.assertEqual(self.comp.residues[var] + self.comp.modulus * kmin,
                             seqs[0])
            self.assertEqual(self.comp.residues[var] + self.comp.modulus * kmax,
                             seqs[-1])
            # extremal witnesses are themselves full satisfying assignments
            self.assertTrue(self.solver.feasible(
                Unary(var, amin[var], amin[var], ())))
            self.assertTrue(self.solver.feasible(
                Unary(var, amax[var], amax[var], ())))

    def test_receipt_verdicts_match_brute_force(self):
        result = run_audit(self.model)
        by_id = {r["receipt_id"]: r for r in result["receipts"]}
        for rc in self.case.receipts:
            expected = _audit_brute(rc, self.model, self.comp, self.brute)
            self.assertEqual(by_id[rc.receipt_id]["verdict"], expected)
        # specific hand expectations
        self.assertEqual(by_id["R1"]["verdict"], "INSUFFICIENT_EVIDENCE")
        self.assertEqual(by_id["R3"]["verdict"], "PROVABLE_NOT_APPROVED")
        self.assertEqual(by_id["R4"]["verdict"], "PROVABLE_NOT_APPROVED")
        # merging the two sources eliminated exactly x(E1)=10
        self.assertEqual(by_id["R1"]["latest_possible_sequence"], 6)
        self.assertEqual(by_id["R1"]["earliest_possible_sequence"], 2)

    def test_witness_assignments_are_complete_and_valid(self):
        result = run_audit(self.model)
        r1 = next(r for r in result["receipts"] if r["receipt_id"] == "R1")
        allow = tuple(sorted(r1["allow_assignment"].items()))
        reject = tuple(sorted(r1["reject_assignment"].items()))
        brute_keys = {tuple(sorted(s.items())) for s in self.brute}
        self.assertIn(allow, brute_keys)
        self.assertIn(reject, brute_keys)


class Fixture2Tests(unittest.TestCase):
    def setUp(self):
        self.case = load_case(os.path.join(
            FIX, "fixture2_repair_ambiguity.json"))
        self.model = build_components(self.case)

    def test_baseline_determination(self):
        result = run_audit(self.model)
        by_id = {r["receipt_id"]: r for r in result["receipts"]}
        self.assertEqual(by_id["R-MAND"]["verdict"], "PROVABLE_APPROVED")
        self.assertEqual(by_id["R-SEALED"]["verdict"],
                         "PROVABLE_NOT_APPROVED")
        self.assertEqual(by_id["R-MAND"]["earliest_possible_sequence"], 2)
        self.assertEqual(by_id["R-MAND"]["latest_possible_sequence"], 2)

    def test_pausing_o2_creates_ambiguity(self):
        comp = self.model.components[0]
        sub = restrict_component(comp, {"o2"})
        solutions = brute_force_solutions(sub, k_ceiling=3)
        e1 = {s["E1"] for s in solutions}
        self.assertEqual(e1, {2, 6})          # determination destroyed
        # mandatory receipt is no longer provably approved under that pause
        predicate = ApprovalPredicate.build(
            "G1", self.model, "E1", "dgx-seal", 2, 4)
        ks = {(s["E1"] - 2) // 4 for s in solutions}
        approved_ks = {k for lo, hi in predicate.allowed_laps()
                       for k in range(lo, hi + 1)}
        self.assertTrue(ks & approved_ks and ks - approved_ks)

    def test_repair_prefers_empty_pause_when_baseline_legal(self):
        out = run_repair(self.model)
        self.assertTrue(out["feasible"])
        self.assertEqual(out["paused_observations"], [])
        self.assertEqual(out["re_verification_cost"], 0)


class ContradictionTests(unittest.TestCase):
    def test_fixture3_minimal_evidence(self):
        case = load_case(os.path.join(FIX, "fixture3_contradiction.json"))
        model = build_components(case)
        result = run_audit(model)
        self.assertEqual(result["contradictions"][0]["minimal_evidence"],
                         ["o2"])
        for rc in result["receipts"]:
            self.assertEqual(rc["verdict"], "CONTRADICTORY_EVIDENCE")

    def test_pausing_contradictory_gap_restores_feasibility(self):
        case = load_case(os.path.join(FIX, "fixture3_contradiction.json"))
        model = build_components(case)
        sub = restrict_component(model.components[0], {"o2"})
        self.assertTrue(Solver(sub).feasible())
        brute = brute_force_solutions(sub, k_ceiling=3)
        self.assertIn({"E0": 0, "E1": 2}, brute)
        self.assertIn({"E0": 0, "E1": 6}, brute)


class NontrivialRepairTests(unittest.TestCase):
    def test_fixture4_unique_minimum(self):
        case = load_case(os.path.join(FIX,
                                      "fixture4_repair_nontrivial.json"))
        model = build_components(case)
        out = run_repair(model)
        self.assertTrue(out["feasible"])
        self.assertEqual(out["paused_observations"], ["o4"])
        self.assertEqual(out["re_verification_cost"], 1)
        # every candidate evaluated earlier must be illegal
        self.assertGreaterEqual(out["candidates_evaluated"], 2)

    def test_fixture4_brute_force_mask_enumeration(self):
        case = load_case(os.path.join(FIX,
                                      "fixture4_repair_nontrivial.json"))
        model = build_components(case)
        ids = case.recheckable
        legal = []
        for mask in range(1 << len(ids)):
            paused = {ids[i] for i in range(len(ids))
                      if mask & (1 << i)}
            sub = restrict_component(model.components[0], paused)
            if not Solver(sub).feasible():
                continue
            solutions = brute_force_solutions(sub)
            if not solutions:
                continue
            e1 = {s["E1"] for s in solutions}
            e2 = {s["E2"] for s in solutions}
            if e1 == {2} and all(x != 2 for x in e2):
                legal.append(sorted(paused))
        costs = dict(case.raw["recheck_costs"])
        legal.sort(key=lambda p: (sum(costs[x] for x in p), len(p),
                                  tuple(p)))
        self.assertEqual(legal[0], ["o4"])

    def test_fixture5_two_components_and_tie_order(self):
        case = load_case(os.path.join(FIX, "fixture5_tie_break.json"))
        model = build_components(case)
        # the two contradictory chains surface as two independent MUS groups
        result = run_audit(model)
        self.assertEqual(
            sorted(tuple(c["minimal_evidence"])
                   for c in result["contradictions"]),
            [("oP",), ("oQ",)])
        out = run_repair(model)
        self.assertEqual(out["paused_observations"], ["oP", "oQ"])
        self.assertEqual(out["re_verification_cost"], 2)


class SameResidueDistinctnessTests(unittest.TestCase):
    """Equal residues must never be merged into one event."""

    def test_equal_residue_events_forced_apart(self):
        data = {
            "case_id": "neq",
            "generations": [{
                "generation_id": "G", "device": "d", "modulus": 4,
                "max_sequence": 9,
                "anchors": [{"anchor_id": "a1", "event_id": "E1",
                             "sequence": 2}],
                "observations": [
                    {"observation_id": "o1", "source_id": "S1",
                     "event_id": "E1", "residue": 2},
                    {"observation_id": "o2", "source_id": "S2",
                     "event_id": "E2", "residue": 2}],
                "approvals": [], "revocations": []}],
            "receipts": [
                {"receipt_id": "R1", "generation_id": "G",
                 "event_id": "E2", "digest": "d"}],
            "recheckable_observations": []}
        model = build_components(parse_case(data))
        comp = model.components[0]
        brute = brute_force_solutions(comp, k_ceiling=3)
        self.assertTrue(all(s["E1"] != s["E2"] for s in brute))
        self.assertTrue(Solver(comp).feasible())
        # E2 can never take the pinned value 2
        self.assertTrue(all(s["E2"] != 2 for s in brute))

    def test_equal_residue_contradiction_when_both_pinned(self):
        data = {
            "case_id": "neq-conflict",
            "generations": [{
                "generation_id": "G", "device": "d", "modulus": 4,
                "max_sequence": 9,
                "anchors": [
                    {"anchor_id": "a1", "event_id": "E1", "sequence": 2},
                    {"anchor_id": "a2", "event_id": "E2", "sequence": 2}],
                "observations": [
                    {"observation_id": "o1", "source_id": "S1",
                     "event_id": "E1", "residue": 2},
                    {"observation_id": "o2", "source_id": "S2",
                     "event_id": "E2", "residue": 2}],
                "approvals": [], "revocations": []}],
            "receipts": [],
            "recheckable_observations": []}
        model = build_components(parse_case(data))
        result = run_audit(model)
        self.assertEqual(len(result["contradictions"]), 1)


class CrossSourceUnificationTests(unittest.TestCase):
    def test_same_event_id_unifies_and_conflicting_residue_rejected(self):
        data = {
            "case_id": "unify",
            "generations": [{
                "generation_id": "G", "device": "d", "modulus": 4,
                "max_sequence": 9,
                "anchors": [],
                "observations": [
                    {"observation_id": "o1", "source_id": "S1",
                     "event_id": "E1", "residue": 2},
                    {"observation_id": "o2", "source_id": "S2",
                     "event_id": "E1", "residue": 3}],
                "approvals": [], "revocations": []}],
            "receipts": [],
            "recheckable_observations": []}
        with self.assertRaises(ModelError):
            build_components(parse_case(data))


class LargeNumberTests(unittest.TestCase):
    """Sequences up to 10**12: no per-sequence or per-loop enumeration."""

    def test_huge_sequences_solved_symbolically(self):
        big = 10**12
        data = {
            "case_id": "big",
            "generations": [{
                "generation_id": "G", "device": "d", "modulus": 4,
                "max_sequence": big,
                "anchors": [
                    {"anchor_id": "a1", "event_id": "E1",
                     "sequence": big - 3}],
                "observations": [
                    {"observation_id": "o1", "source_id": "S",
                     "event_id": "E1", "residue": (big - 3) % 4},
                    {"observation_id": "o2", "source_id": "S",
                     "event_id": "E2", "residue": 2,
                     "gap_min": 1, "gap_max": 2}],
                "approvals": [
                    {"lo": big - 2, "hi": big - 2, "digest": "d",
                     "evidence_id": "ap1"}],
                "revocations": []}],
            "receipts": [
                {"receipt_id": "R1", "generation_id": "G",
                 "event_id": "E2", "digest": "d", "mandatory": True}],
            "recheckable_observations": []}
        model = build_components(parse_case(data))
        result = run_audit(model)
        r1 = result["receipts"][0]
        self.assertEqual(r1["verdict"], "PROVABLE_APPROVED")
        self.assertEqual(r1["earliest_possible_sequence"], big - 2)
        self.assertEqual(r1["latest_possible_sequence"], big - 2)


class VerifyTests(unittest.TestCase):
    def test_verify_accepts_genuine_audit_and_rejects_tampered(self):
        case = load_case(os.path.join(FIX,
                                      "fixture1_two_lap_two_source.json"))
        model = build_components(case)
        genuine = run_audit(model)
        verdict = run_verify(model, genuine)
        self.assertTrue(verdict["accepted"])

        tampered = json.loads(json.dumps(genuine))
        tampered["receipts"][0]["verdict"] = "PROVABLE_APPROVED"
        verdict = run_verify(model, tampered)
        self.assertFalse(verdict["accepted"])

    def test_verify_rejects_one_sided_witness(self):
        case = load_case(os.path.join(FIX,
                                      "fixture1_two_lap_two_source.json"))
        model = build_components(case)
        claim = run_audit(model)
        r1 = next(r for r in claim["receipts"]
                  if r["receipt_id"] == "R1")
        del r1["reject_assignment"]
        verdict = run_verify(model, claim)
        self.assertFalse(verdict["accepted"])
        self.assertTrue(any("one-sided" in e["error"]
                            for e in verdict["errors"]))

    def test_verify_rejects_residue_conflation_witness(self):
        case = load_case(os.path.join(FIX,
                                      "fixture1_two_lap_two_source.json"))
        model = build_components(case)
        claim = run_audit(model)
        r1 = next(r for r in claim["receipts"]
                  if r["receipt_id"] == "R1")
        # forge a witness that gives E1 and E2 the same actual sequence
        r1["allow_assignment"] = {"E1": 6, "E2": 6, "E3": 10}
        verdict = run_verify(model, claim)
        self.assertFalse(verdict["accepted"])
        self.assertTrue(any("residue" in e["error"] or "merges" in e["error"]
                            for e in verdict["errors"]))

    def test_verify_rejects_later_same_cost_repair(self):
        case = load_case(os.path.join(FIX,
                                      "fixture4_repair_nontrivial.json"))
        model = build_components(case)
        good = run_repair(model)
        self.assertTrue(run_verify(model, good)["accepted"])
        worse = dict(good)
        worse["paused_observations"] = ["o4", "o6"]
        worse["re_verification_cost"] = 11
        verdict = run_verify(model, worse)
        self.assertFalse(verdict["accepted"])

    def test_verify_accepts_fixture5_repair(self):
        case = load_case(os.path.join(FIX, "fixture5_tie_break.json"))
        model = build_components(case)
        good = run_repair(model)
        self.assertTrue(run_verify(model, good)["accepted"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
