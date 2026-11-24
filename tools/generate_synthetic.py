"""Deterministic synthetic case generator.

Produces three kinds of synthetic input, all fully derived from integer seeds
(no randomness, byte-stable output):

* ``aligned``  - one long source whose every gap is tight enough to pin every
  event; demonstrates the 200k-log path where no exact search is needed;
* ``ambiguous`` - a small component (at most 16 undetermined events) attached
  to an aligned prefix, exercising the exact solver;
* ``repair``   - a repairable contradiction with recheckable observations.

The generated aligned component has zero ambiguous variables by construction,
so audit work stays linear in the log size regardless of sequence magnitude.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def aligned_case(case_id: str, n_events: int, modulus: int = 4,
                 big_stride: bool = False) -> dict:
    """Tight-gap chain: x_{t+1} - x_t is a fixed admissible residue step."""
    M = modulus
    stride = M                      # same residue every event, distinct x
    start = 10**12 - (n_events * M) if big_stride else 0
    if start < 0:
        start = 0
    observations = [{
        "observation_id": "o0", "source_id": "S1",
        "event_id": "E0", "residue": start % M}]
    for t in range(1, n_events):
        observations.append({
            "observation_id": f"o{t}", "source_id": "S1",
            "event_id": f"E{t}", "residue": start % M,
            "gap_min": stride, "gap_max": stride})
    max_sequence = start + (n_events + 2) * M
    approvals = [{
        "lo": start + stride, "hi": start + stride,
        "digest": "dgx-aligned", "evidence_id": "app1"}]
    receipts = [{
        "receipt_id": "R-tail", "generation_id": "G1",
        "event_id": f"E{n_events - 1}", "digest": "dgx-other"}]
    return {
        "case_id": case_id,
        "generations": [{
            "generation_id": "G1", "device": "dev-aligned",
            "modulus": M, "max_sequence": max_sequence,
            "anchors": [{"anchor_id": "a0", "event_id": "E0",
                         "sequence": start}],
            "observations": observations,
            "approvals": approvals, "revocations": []}],
        "receipts": receipts,
        "recheckable_observations": []}


def ambiguous_case(case_id: str, n_undetermined: int = 4) -> dict:
    """Aligned prefix plus a small ambiguous component with loose gaps."""
    assert 1 <= n_undetermined <= 16
    M = 4
    observations = [
        {"observation_id": "o0", "source_id": "S1",
         "event_id": "A0", "residue": 0}]
    for t in range(1, 8):
        observations.append({
            "observation_id": f"oa{t}", "source_id": "S1",
            "event_id": f"A{t}", "residue": 0,
            "gap_min": M, "gap_max": M})
    # ambiguous island: same residue, loose gaps allow several laps
    observations.append({
        "observation_id": "ob0", "source_id": "S2",
        "event_id": "U0", "residue": 2})
    for t in range(1, n_undetermined):
        observations.append({
            "observation_id": f"ob{t}", "source_id": "S2",
            "event_id": f"U{t}", "residue": 2,
            "gap_min": 1, "gap_max": 4 + 4 * n_undetermined})
    return {
        "case_id": case_id,
        "generations": [{
            "generation_id": "G1", "device": "dev-ambiguous",
            "modulus": M, "max_sequence": 4 * (n_undetermined + 4),
            "anchors": [{"anchor_id": "a0", "event_id": "A0",
                         "sequence": 0}],
            "observations": observations,
            "approvals": [{"lo": 2, "hi": 6, "digest": "dgx-island",
                           "evidence_id": "app1"}],
            "revocations": []}],
        "receipts": [{
            "receipt_id": "R-U0", "generation_id": "G1",
            "event_id": "U0", "digest": "dgx-island"}],
        "recheckable_observations": []}


def repair_case(case_id: str) -> dict:
    """Self-contradictory gap fixed by pausing one recheckable observation."""
    return {
        "case_id": case_id,
        "generations": [{
            "generation_id": "G1", "device": "dev-repair",
            "modulus": 4, "max_sequence": 9,
            "anchors": [
                {"anchor_id": "a1", "event_id": "E0", "sequence": 0},
                {"anchor_id": "a2", "event_id": "E1", "sequence": 2}],
            "observations": [
                {"observation_id": "o1", "source_id": "S1",
                 "event_id": "E0", "residue": 0},
                {"observation_id": "o2", "source_id": "S1",
                 "event_id": "E1", "residue": 2,
                 "gap_min": 3, "gap_max": 4}],
            "approvals": [{"lo": 2, "hi": 2, "digest": "dgx-seal",
                           "evidence_id": "app1"}],
            "revocations": []}],
        "receipts": [{
            "receipt_id": "R1", "generation_id": "G1",
            "event_id": "E1", "digest": "dgx-seal", "mandatory": True}],
        "recheckable_observations": ["o2"],
        "recheck_costs": {"o2": 1}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Synthetic case generator")
    parser.add_argument("kind", choices=["aligned", "ambiguous", "repair"])
    parser.add_argument("-n", "--events", type=int, default=200_000)
    parser.add_argument("--undetermined", type=int, default=4)
    parser.add_argument("--big", action="store_true",
                        help="scale sequences toward 10**12")
    parser.add_argument("-o", "--output", required=True)
    args = parser.parse_args(argv)

    if args.kind == "aligned":
        payload = aligned_case(f"syn-{args.kind}", args.events,
                               big_stride=args.big)
    elif args.kind == "ambiguous":
        payload = ambiguous_case(f"syn-{args.kind}", args.undetermined)
    else:
        payload = repair_case(f"syn-{args.kind}")

    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
