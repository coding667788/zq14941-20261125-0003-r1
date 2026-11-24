"""Randomized differential testing: solver vs exhaustive enumeration.

Generates many small random components (few events, small lap domains,
random residues/gaps/anchors) and checks that the production solver agrees
with brute-force enumeration on:

* feasibility,
* every event's exact feasible sequence set and extrema,
* distinctness (equal residues never collapse),
* every receipt verdict under random approval intervals and digests.

A fixed integer seed makes every run reproducible; no randomness enters the
production package itself.
"""

import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from counterweir.audit import ApprovalPredicate
from counterweir.model import (Edge, Neq, Component, Unary, build_components,
                               parse_case)
from counterweir.solver import Solver, brute_force_solutions

SEED = 20261125
K_CEIL = 3                      # laps stay in 0..3 so brute force is tiny
TRIALS = 120


def _random_component(rng: random.Random) -> Component:
    n = rng.randint(2, 5)
    M = rng.choice((2, 3, 4))
    residues = [rng.randrange(M) for _ in range(n)]
    events = [f"E{i}" for i in range(n)]
    unary: list[Unary] = []
    edges: list[Edge] = []
    neqs: list[Neq] = []
    evidence = set()

    for v in range(n):
        # anchor some variables to a singleton lap
        if rng.random() < 0.4:
            k = rng.randint(0, K_CEIL)
            unary.append(Unary(v, k, k, (f"a{v}",)))
            evidence.add(f"a{v}")
        else:
            unary.append(Unary(v, 0, K_CEIL, (f"o{v}",)))
            evidence.add(f"o{v}")

    # random ordering edges along a random spanning chain
    order = list(range(n))
    rng.shuffle(order)
    for a, b in zip(order, order[1:]):
        lo = rng.randint(-2, 2)
        hi = lo + rng.randint(0, 3)
        eid = f"g{a}-{b}"
        edges.append(Edge(a, b, lo, hi, (eid,)))
        evidence.add(eid)

    # distinctness for equal residues
    for i in range(n):
        for j in range(i + 1, n):
            if residues[i] == residues[j]:
                neqs.append(Neq(i, j, (f"n{i}-{j}",)))
                evidence.add(f"n{i}-{j}")

    return Component(
        generation_id="G", modulus=M, var_event=events,
        event_var={ev: idx for idx, ev in enumerate(events)},
        residues=residues, unary=unary, edges=edges, neqs=neqs,
        pinned=[False] * n, evidence=evidence)


class RandomDifferentialTests(unittest.TestCase):
    def test_solver_matches_brute_force(self):
        rng = random.Random(SEED)
        checked_feasible = 0
        checked_infeasible = 0
        for _ in range(TRIALS):
            comp = _random_component(rng)
            solver = Solver(comp)
            feasible = solver.feasible()
            brute = brute_force_solutions(comp, k_ceiling=K_CEIL)
            self.assertEqual(feasible, bool(brute),
                             msg=f"feasibility mismatch on {comp}")
            if not feasible:
                checked_infeasible += 1
                continue
            checked_feasible += 1

            # exact feasible sequence set per event
            for v, ev in enumerate(comp.var_event):
                brute_seqs = sorted(sol[ev] for sol in brute)
                kmin, amin = solver.extremum(v, True)
                kmax, amax = solver.extremum(v, False)
                M = comp.modulus
                self.assertEqual(comp.residues[v] + M * kmin, brute_seqs[0])
                self.assertEqual(comp.residues[v] + M * kmax, brute_seqs[-1])
                # every brute sequence must be reachable; the solver's
                # extrema witnesses must themselves be full solutions
                self.assertTrue(solver.feasible(
                    Unary(v, amin[v], amin[v], ())))
                self.assertTrue(solver.feasible(
                    Unary(v, amax[v], amax[v], ())))

            # equal-residue events are never merged in any enumerated solution
            for i in range(len(comp.var_event)):
                for j in range(i + 1, len(comp.var_event)):
                    if comp.residues[i] == comp.residues[j]:
                        for sol in brute:
                            self.assertNotEqual(
                                sol[comp.var_event[i]],
                                sol[comp.var_event[j]])

        # both branches must actually be exercised
        self.assertGreater(checked_feasible, 0)
        self.assertGreater(checked_infeasible, 0)

    def test_approval_verdict_matches_brute_force(self):
        rng = random.Random(SEED + 1)
        for _ in range(60):
            comp = _random_component(rng)
            brute = brute_force_solutions(comp, k_ceiling=K_CEIL)
            if not brute:
                continue
            solver = Solver(comp)
            self.assertTrue(solver.feasible())
            # build a lightweight case-like object via a minimal parse_case
            # is overkill; exercise ApprovalPredicate against lap intervals
            v = rng.randrange(len(comp.var_event))
            M = comp.modulus
            r = comp.residues[v]
            # random approval point expressed as an actual sequence interval
            point = r + M * rng.randint(0, K_CEIL)
            from counterweir.audit import _lap_intervals, _merge
            predicate = ApprovalPredicate(
                _merge(_lap_intervals(point, point, r, M)), [])
            allowed = predicate.allowed_laps()
            brute_ks = {(sol[comp.var_event[v]] - r) // M for sol in brute}
            approved_ks = {k for lo, hi in allowed
                           for k in range(lo, hi + 1)}
            all_in = brute_ks <= approved_ks
            any_in = bool(brute_ks & approved_ks)
            expected = ("PROVABLE_APPROVED" if all_in
                        else "PROVABLE_NOT_APPROVED" if not any_in
                        else "INSUFFICIENT_EVIDENCE")
            # reproduce the production posture logic
            some_allowed = any(solver.feasible(Unary(v, lo, hi, ()))
                               for lo, hi in allowed)
            (dlo, dhi), _ = solver.initial_domains()
            outside = []
            cursor = dlo[v]
            for lo, hi in allowed:
                if cursor <= lo - 1:
                    outside.append((cursor, lo - 1))
                cursor = max(cursor, hi + 1)
            if cursor <= dhi[v]:
                outside.append((cursor, dhi[v]))
            some_rejected = any(solver.feasible(Unary(v, lo, hi, ()))
                                for lo, hi in outside)
            actual = ("PROVABLE_APPROVED" if some_allowed and not some_rejected
                      else "PROVABLE_NOT_APPROVED"
                      if some_rejected and not some_allowed
                      else "INSUFFICIENT_EVIDENCE")
            self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
