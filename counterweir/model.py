"""Input model: parse a counterweir case into per-generation constraint components.

A *variable* is an event occurrence, keyed by (generation_id, event_id).  Every
observation contributes the residue ``x mod M`` of the event's actual sequence
``x``; anchors pin ``x`` exactly.  Source collection order contributes
difference inequalities on consecutive observations.  Same-generation events
with equal residues that are *not* the same event are additionally required to
take distinct actual sequences.

Everything here is integer arithmetic only; sequences may reach 10**12 and no
loop is ever unrolled.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional


class ModelError(ValueError):
    """Raised for malformed input declarations."""


# --------------------------------------------------------------------------- #
# Parsed declarations
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Observation:
    obs_id: str
    generation_id: str
    event_id: str
    source_id: str
    ordinal: int                 # position within the source, ascending
    residue: int
    gap_min: Optional[int]       # bounds on x(this) - x(prev_in_source)
    gap_max: Optional[int]


@dataclass(frozen=True)
class Anchor:
    anchor_id: str
    generation_id: str
    event_id: str
    sequence: int


@dataclass(frozen=True)
class Interval:
    lo: int
    hi: int
    digest: str
    evidence_id: str


@dataclass(frozen=True)
class Receipt:
    receipt_id: str
    generation_id: str
    event_id: str
    digest: str
    mandatory: bool = False
    sealed: bool = False


@dataclass
class Generation:
    generation_id: str
    modulus: int
    device: str                  # canonical device name after alias resolution
    max_sequence: Optional[int]
    observations: list[Observation] = field(default_factory=list)
    anchors: list[Anchor] = field(default_factory=list)
    approvals: list[Interval] = field(default_factory=list)
    revocations: list[Interval] = field(default_factory=list)


@dataclass
class Case:
    case_id: str
    generations: dict[str, Generation]
    receipts: list[Receipt]
    recheckable: list[str]       # observation ids usable by repair
    raw: dict


# --------------------------------------------------------------------------- #
# Normalized constraints (local to one component)
# --------------------------------------------------------------------------- #

@dataclass
class Unary:
    """``lo <= k_i <= hi`` on the lap variable ``x_i = r_i + M*k_i``."""
    var: int
    lo: int
    hi: int
    evidence: tuple[str, ...]


@dataclass
class Edge:
    """``lo <= k_j - k_i <= hi``."""
    i: int
    j: int
    lo: int
    hi: int
    evidence: tuple[str, ...]


@dataclass
class Neq:
    """``x_i != x_j`` (only generated for equal-residue pairs)."""
    i: int
    j: int
    evidence: tuple[str, ...]


@dataclass
class Component:
    generation_id: str
    modulus: int
    var_event: list[str]                     # local idx -> event id
    event_var: dict[str, int]
    residues: list[int]
    unary: list[Unary]
    edges: list[Edge]
    neqs: list[Neq]
    pinned: list[bool]                       # exact single value (anchor)
    evidence: set[str]
    deterministic: bool = False              # propagation yields singletons


@dataclass
class BuiltModel:
    case: Case
    components: list[Component]
    comp_of: dict[tuple[str, str], int]      # (generation, event) -> component


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #

def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise ModelError(msg)


def load_case(path: str) -> Case:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return parse_case(data)


def parse_case(data: dict) -> Case:
    _require(isinstance(data, dict), "top-level JSON must be an object")
    case_id = str(data.get("case_id", "case"))
    _require(isinstance(data.get("generations"), list) and data["generations"],
             "generations must be a non-empty list")

    gens: dict[str, Generation] = {}
    for gdecl in data["generations"]:
        gid = str(gdecl["generation_id"])
        _require(gid not in gens, f"duplicate generation_id {gid}")
        M = int(gdecl["modulus"])
        _require(M >= 1, f"generation {gid}: modulus must be >= 1")
        aliases = gdecl.get("device_aliases", {}) or {}
        _require(isinstance(aliases, dict), f"generation {gid}: bad alias map")
        canonical = gdecl["device"]
        device = canonical
        # aliases only resolve within this generation's effective mapping
        if device in aliases:
            device = aliases[device]
        max_seq = gdecl.get("max_sequence")
        if max_seq is not None:
            max_seq = int(max_seq)
            _require(max_seq >= 0, f"generation {gid}: bad max_sequence")
        gen = Generation(gid, M, device, max_seq)

        for adecl in gdecl.get("anchors", []):
            seq = int(adecl["sequence"])
            _require(seq >= 0, f"anchor {adecl['anchor_id']}: negative sequence")
            if max_seq is not None:
                _require(seq <= max_seq,
                         f"anchor {adecl['anchor_id']}: sequence above max_sequence")
            gen.anchors.append(Anchor(
                str(adecl["anchor_id"]), gid, str(adecl["event_id"]), seq))

        # group observations by source, preserve declared collection order
        per_source: dict[str, list[dict]] = {}
        for odecl in gdecl.get("observations", []):
            per_source.setdefault(str(odecl["source_id"]), []).append(odecl)
        seen_obs: set[str] = set()
        for sid, olist in per_source.items():
            default_min = gdecl.get("source_gap_defaults", {}).get(sid, {}).get("min")
            default_max = gdecl.get("source_gap_defaults", {}).get(sid, {}).get("max")
            prev_event = None
            for ordinal, odecl in enumerate(olist):
                oid = str(odecl["observation_id"])
                _require(oid not in seen_obs, f"duplicate observation_id {oid}")
                seen_obs.add(oid)
                ev = str(odecl["event_id"])
                r = int(odecl["residue"])
                _require(0 <= r < M,
                         f"observation {oid}: residue outside [0, M)")
                gmin = odecl.get("gap_min", default_min)
                gmax = odecl.get("gap_max", default_max)
                if ordinal > 0:
                    _require(gmin is not None and gmax is not None,
                             f"observation {oid}: missing gap bounds vs predecessor")
                    gmin, gmax = int(gmin), int(gmax)
                    _require(gmin <= gmax,
                             f"observation {oid}: bad gap bounds")
                else:
                    _require(gmin is None and gmax is None,
                             f"observation {oid}: first event in source has no gap")
                if prev_event is not None and prev_event == ev:
                    raise ModelError(
                        f"source {sid}: event {ev} collected twice in order; "
                        "same-generation actual sequences must differ")
                gen.observations.append(Observation(
                    oid, gid, ev, sid, ordinal, r,
                    gmin if ordinal > 0 else None,
                    gmax if ordinal > 0 else None))
                prev_event = ev

        for kind in ("approvals", "revocations"):
            for idecl in gdecl.get(kind, []):
                lo, hi = int(idecl["lo"]), int(idecl["hi"])
                _require(0 <= lo <= hi, f"generation {gid}: bad {kind} interval")
                if max_seq is not None:
                    _require(hi <= max_seq,
                             f"generation {gid}: {kind} interval above max_sequence")
                getattr(gen, kind).append(Interval(
                    lo, hi, str(idecl["digest"]),
                    str(idecl.get("evidence_id", f"{kind}:{lo}:{hi}"))))
        gens[gid] = gen

    receipts: list[Receipt] = []
    for rdecl in data.get("receipts", []):
        rid = str(rdecl["receipt_id"])
        gid = str(rdecl["generation_id"])
        _require(gid in gens, f"receipt {rid}: unknown generation {gid}")
        receipts.append(Receipt(
            rid, gid, str(rdecl["event_id"]), str(rdecl["digest"]),
            bool(rdecl.get("mandatory", False)),
            bool(rdecl.get("sealed", False))))

    recheckable = [str(x) for x in data.get("recheckable_observations", [])]
    known_obs = {o.obs_id for g in gens.values() for o in g.observations}
    for oid in recheckable:
        _require(oid in known_obs, f"recheckable unknown observation {oid}")
    _require(len(recheckable) <= 16, "at most 16 recheckable observations allowed")

    return Case(case_id, gens, receipts, recheckable, data)


# --------------------------------------------------------------------------- #
# Component construction
# --------------------------------------------------------------------------- #

class _DSU:
    def __init__(self, n: int):
        self.p = list(range(n))

    def find(self, x: int) -> int:
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


def build_components(case: Case) -> BuiltModel:
    """Turn declarations into per-generation constraint components.

    Raises :class:`ModelError` for declaration-level inconsistencies that are
    visible without solving (duplicate residues for one event, unknown receipt
    events, oversized ambiguous components).
    """
    components: list[Component] = []
    comp_of: dict[tuple[str, str], int] = {}

    for gid, gen in case.generations.items():
        M = gen.modulus
        # variable table keyed by event id; cross-source same-id == same var
        event_var: dict[str, int] = {}
        var_event: list[str] = []
        residues: dict[str, int] = {}
        var_evidence: dict[str, list[str]] = {}

        def var_for(ev: str) -> int:
            if ev not in event_var:
                event_var[ev] = len(var_event)
                var_event.append(ev)
                var_evidence[ev] = []
            return event_var[ev]

        unary_by_var: dict[int, list[Unary]] = {}
        gen_bound: dict[int, int] = {}     # non-pausable generation ceiling
        raw_edges: list[Edge] = []
        anchor_seq: dict[str, int] = {}
        bound_eid = f"gen-bound:{gid}"

        for o in gen.observations:
            v = var_for(o.event_id)
            var_evidence[o.event_id].append(o.obs_id)
            if o.event_id in residues:
                if residues[o.event_id] != o.residue:
                    raise ModelError(
                        f"event {o.event_id} in generation {gid}: conflicting "
                        f"residues {residues[o.event_id]} vs {o.residue} "
                        "(same stable event id must be one actual sequence)")
            else:
                residues[o.event_id] = o.residue
            # The observation attests the residue (k >= 0); the numeric
            # ceiling is the generation's own max_sequence and survives even
            # if this observation is later paused during repair.
            unary_by_var.setdefault(v, []).append(
                Unary(v, 0, 10**18 // M, (o.obs_id,)))
            if gen.max_sequence is not None:
                gen_bound[v] = (gen.max_sequence - o.residue) // M

        for a in gen.anchors:
            v = var_for(a.event_id)
            var_evidence[a.event_id].append(a.anchor_id)
            if a.event_id in residues:
                if a.sequence % M != residues[a.event_id]:
                    raise ModelError(
                        f"anchor {a.anchor_id}: sequence {a.sequence} residue "
                        f"disagrees with observed residue of event {a.event_id}")
            else:
                residues[a.event_id] = a.sequence % M
            if a.event_id in anchor_seq and anchor_seq[a.event_id] != a.sequence:
                raise ModelError(
                    f"event {a.event_id}: two anchors pin different sequences")
            anchor_seq[a.event_id] = a.sequence
            k = (a.sequence - a.sequence % M) // M
            unary_by_var.setdefault(v, []).append(Unary(v, k, k, (a.anchor_id,)))

        # source ordering edges, expressed on lap variables
        per_source: dict[str, list[Observation]] = {}
        for o in gen.observations:
            per_source.setdefault(o.source_id, []).append(o)
        for olist in per_source.values():
            olist.sort(key=lambda o: o.ordinal)
            for prev, cur in zip(olist, olist[1:]):
                i, j = event_var[prev.event_id], event_var[cur.event_id]
                dmin, dmax = cur.gap_min, cur.gap_max
                ri, rj = residues[prev.event_id], residues[cur.event_id]
                # dmin <= (rj - ri) + M*(kj - ki) <= dmax
                shifted_min = dmin - (rj - ri)
                lo_k = -((-shifted_min) // M)          # ceil(shifted_min / M)
                hi_k = (dmax - (rj - ri)) // M         # floor
                if i == j:
                    raise ModelError(
                        f"source {cur.source_id}: event {cur.event_id} repeats; "
                        "actual sequences within a generation must be distinct")
                raw_edges.append(Edge(i, j, lo_k, hi_k, (cur.obs_id,)))

        # events grouped by residue; same-residue distinctness pairs are
        # derived lazily and only for small/ambiguous components.  A large
        # aligned tight-gap chain already forces every sequence apart, so
        # building O(n^2) pairs there is both pointless and quadratic.
        by_res: dict[int, list[str]] = {}
        for ev, r in residues.items():
            by_res.setdefault(r, []).append(ev)
        for evs in by_res.values():
            evs.sort()

        # receipts must refer to known events
        for rc in case.receipts:
            if rc.generation_id == gid and rc.event_id not in event_var:
                raise ModelError(
                    f"receipt {rc.receipt_id}: event {rc.event_id} never observed "
                    f"or anchored in generation {gid}")

        from .solver import Infeasible as _Infeasible  # local import
        from .solver import Solver as _Solver

        n = len(event_var)

        # phase 1: candidate groups over ordering edges only
        edge_dsu = _DSU(n)
        for e in raw_edges:
            edge_dsu.union(e.i, e.j)
        edge_groups: dict[int, list[int]] = {}
        for v in range(n):
            edge_groups.setdefault(edge_dsu.find(v), []).append(v)

        def assemble(member_idx: list[int],
                     with_neqs: bool) -> Component:
            member_idx = sorted(member_idx)
            local = {old: idx for idx, old in enumerate(member_idx)}
            comp = Component(
                generation_id=gid, modulus=M,
                var_event=[var_event[old] for old in member_idx],
                event_var={var_event[old]: idx
                           for idx, old in enumerate(member_idx)},
                residues=[residues[var_event[old]] for old in member_idx],
                unary=[], edges=[], neqs=[],
                pinned=[var_event[old] in anchor_seq for old in member_idx],
                evidence=set())
            member_set = set(member_idx)
            for old in member_idx:
                for u in unary_by_var.get(old, []):
                    nu = Unary(local[u.var], u.lo, u.hi, u.evidence)
                    comp.unary.append(nu)
                    comp.evidence.update(u.evidence)
                # generation ceiling: independent of any recheckable
                # observation, so it cannot be paused away during repair
                if old in gen_bound:
                    bu = Unary(local[old], 0, gen_bound[old], (bound_eid,))
                    comp.unary.append(bu)
                    comp.evidence.add(bound_eid)
            for e in raw_edges:
                if e.i in member_set and e.j in member_set:
                    comp.edges.append(
                        Edge(local[e.i], local[e.j], e.lo, e.hi, e.evidence))
                    comp.evidence.update(e.evidence)
            if with_neqs:
                local_events = {var_event[old] for old in member_idx}
                # derive distinctness pairs only inside this small component
                local_by_res: dict[int, list[str]] = {}
                for ev in local_events:
                    local_by_res.setdefault(residues[ev], []).append(ev)
                for evs in local_by_res.values():
                    evs.sort()
                    for a in range(len(evs)):
                        for b in range(a + 1, len(evs)):
                            ev1, ev2 = evs[a], evs[b]
                            nq = Neq(local[event_var[ev1]],
                                     local[event_var[ev2]],
                                     tuple(sorted(var_evidence[ev1]
                                                  + var_evidence[ev2])))
                            comp.neqs.append(nq)
                            comp.evidence.update(nq.evidence)
            return comp

        def is_deterministic(comp: Component) -> bool:
            probe = _Solver(comp)
            (plo, phi), pex = probe.initial_domains()
            try:
                probe.propagate([plo, phi], pex,
                                list(range(len(comp.var_event))))
            except _Infeasible:
                return False
            return all(plo[v] == phi[v]
                       for v in range(len(comp.var_event)))

        # Large deterministic edge chains are emitted without O(n^2) neqs,
        # but only after proving every propagated sequence is actually
        # distinct per residue class (the distinctness the skipped neqs
        # would otherwise enforce).  A collision routes the group into the
        # small-component pool so the contradiction is surfaced.
        pooled: set[int] = set()
        for members in edge_groups.values():
            edge_only = assemble(members, with_neqs=False)
            deterministic = is_deterministic(edge_only)
            collision = False
            if deterministic and len(members) > 16:
                probe = _Solver(edge_only)
                (plo, phi), _ = probe.initial_domains()
                probe.propagate([plo, phi], [set() for _ in members],
                                list(range(len(members))))
                seen: dict[tuple[int, int], int] = {}
                for v in range(len(members)):
                    key = (edge_only.residues[v], plo[v])
                    if key in seen:
                        collision = True
                        break
                    seen[key] = v
            if deterministic and len(members) > 16 and not collision:
                edge_only.deterministic = True
                cidx = len(components)
                components.append(edge_only)
                for old in members:
                    comp_of[(gid, var_event[old])] = cidx
            else:
                if not deterministic:
                    undetermined = len(members)
                    if undetermined > 16:
                        raise ModelError(
                            f"generation {gid}: ambiguous component with "
                            f"{undetermined} undetermined events exceeds the "
                            "16-event exact-solve limit")
                pooled.update(members)

        # phase 2: link pooled variables by BOTH ordering edges and
        # same-residue distinctness (the edge topology must be preserved;
        # neqs only add extra links between equal-residue events)
        pool_dsu = _DSU(n)
        for e in raw_edges:
            if e.i in pooled and e.j in pooled:
                pool_dsu.union(e.i, e.j)
        for evs in by_res.values():
            pooled_evs = [ev for ev in evs if event_var[ev] in pooled]
            for ev1, ev2 in zip(pooled_evs, pooled_evs[1:]):
                pool_dsu.union(event_var[ev1], event_var[ev2])
        final_groups: dict[int, list[int]] = {}
        for v in pooled:
            final_groups.setdefault(pool_dsu.find(v), []).append(v)

        for members in final_groups.values():
            comp = assemble(members, with_neqs=True)
            comp.deterministic = is_deterministic(comp)
            cidx = len(components)
            components.append(comp)
            for old in members:
                comp_of[(gid, var_event[old])] = cidx

    return BuiltModel(case, components, comp_of)


def restrict_component(c: Component, paused: set[str]) -> Component:
    """Rebuild a component dropping every constraint backed by paused evidence.

    Pausing an observation removes its residue unary, its predecessor edge and
    every derived distinctness pair depending on it.  Anchors are never
    recheckable and therefore never removed.
    """
    out = Component(
        generation_id=c.generation_id, modulus=c.modulus,
        var_event=list(c.var_event), event_var=dict(c.event_var),
        residues=list(c.residues), unary=[], edges=[], neqs=[],
        pinned=list(c.pinned), evidence=set())
    for u in c.unary:
        if not any(e in paused for e in u.evidence):
            out.unary.append(Unary(u.var, u.lo, u.hi, u.evidence))
            out.evidence.update(u.evidence)
    for e in c.edges:
        if not any(x in paused for x in e.evidence):
            out.edges.append(Edge(e.i, e.j, e.lo, e.hi, e.evidence))
            out.evidence.update(e.evidence)
    for q in c.neqs:
        if not any(x in paused for x in q.evidence):
            out.neqs.append(Neq(q.i, q.j, q.evidence))
            out.evidence.update(q.evidence)
    return out
