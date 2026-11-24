"""Exact solver for one constraint component.

Each event actual sequence is ``x_i = r_i + M * k_i`` with ``k_i >= 0``.
All order/gap constraints become pure difference constraints
``lo <= k_j - k_i <= hi``; anchors become singleton unary domains; same
residue distinctness becomes ``k_i != k_j`` (equivalent to ``x_i != x_j``
because residues match).

Propagation is AC-3 style bounds propagation on difference constraints plus
pruning of assigned disequality partners.  Domains stay intervals throughout,
so propagation is linear in the constraint count.  Search only branches on
variables whose disequality partners are assigned; interval constraints alone
always leave an interval domain, hence a contiguous set of feasible laps.

No actual-sequence or loop enumeration ever happens: even with
``max_sequence = 10**12`` the work is proportional to component size.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass

from .model import Component, Unary


class Infeasible(Exception):
    pass


@dataclass
class _Adj:
    """Outgoing incidence: edge (u -> v) with ``lo <= k_v - k_u <= hi``."""
    v: int
    lo: int
    hi: int


class Solver:
    def __init__(self, comp: Component):
        self.comp = comp
        self.n = len(comp.var_event)
        self.M = comp.modulus
        self.adj: list[list[_Adj]] = [[] for _ in range(self.n)]
        for e in comp.edges:
            if e.lo > e.hi:
                # a difference constraint with an empty interval can never
                # hold (typically produced by an impossible residue/gap pair);
                # mark via a self-loop that bounds propagation will reject
                self.adj[e.i].append(_Adj(e.i, 1, 0))
            else:
                self.adj[e.i].append(_Adj(e.j, e.lo, e.hi))
                self.adj[e.j].append(_Adj(e.i, -e.hi, -e.lo))
        self.adj_flat = sum(len(a) for a in self.adj)
        self.neqs = list(comp.neqs)
        self.neq_of: list[list[int]] = [[] for _ in range(self.n)]
        for idx, q in enumerate(self.neqs):
            self.neq_of[q.i].append(idx)
            self.neq_of[q.j].append(idx)

    # ------------------------------------------------------------------ #
    # Domain initialization and propagation
    # ------------------------------------------------------------------ #

    def initial_domains(self) -> tuple[list[list[int]], list[set[int]]]:
        lo = [0] * self.n
        hi = [10**18] * self.n
        for u in self.comp.unary:
            lo[u.var] = max(lo[u.var], u.lo)
            hi[u.var] = min(hi[u.var], u.hi)
        excluded = [set() for _ in range(self.n)]
        return [lo, hi], excluded

    def propagate(self, dom: list[list[int]], excluded: list[set[int]],
                  queue: list[int]) -> None:
        """Narrow domains until a fixed point; raise Infeasible on emptiness.

        Uses a plain FIFO relaxation queue (Bellman-Ford style).  A node may
        be enqueued more than once, but each dequeue relaxes its incidence
        edges once; for difference-constraint systems the number of
        relaxations is linear in the constraint graph, so a 200k-event
        aligned chain stays O(n).
        """
        lo, hi = dom
        # FIFO relaxation queue with per-node pending dedup: a node is only
        # queued when one of its bounds or its excluded set actually changed,
        # and a single pending entry drains all queued updates at once.
        pending = [False] * self.n
        q = list(queue)
        for v in q:
            pending[v] = True
        # Bellman-Ford bound: a feasible system converges within V*E bound
        # relaxations.  Exceeding it proves a positive-width (unsatisfiable)
        # cycle, so we stop instead of looping.  Generous constant factor
        # accounts for the disequality-driven re-enqueues.
        relaxation_cap = 4 * self.n * max(1, self.adj_flat) + self.n * 4
        relaxations = 0
        head = 0
        while head < len(q):
            u = q[head]
            head += 1
            pending[u] = False

            def enqueue(v: int) -> None:
                if not pending[v]:
                    pending[v] = True
                    q.append(v)

            for a in self.adj[u]:
                v = a.v
                changed = False
                new_lo = lo[u] + a.lo
                if new_lo > lo[v]:
                    lo[v] = new_lo
                    changed = True
                new_hi = hi[u] + a.hi
                if new_hi < hi[v]:
                    hi[v] = new_hi
                    changed = True
                if lo[v] > hi[v]:
                    raise Infeasible
                if changed:
                    relaxations += 1
                    if relaxations > relaxation_cap:
                        raise Infeasible
                    enqueue(v)
            # disequality pruning: a singleton domain forbids its value on
            # every partner; only enqueue when a new exclusion is added
            if lo[u] == hi[u]:
                val = lo[u]
                for idx in self.neq_of[u]:
                    q2 = self.neqs[idx]
                    v = q2.j if q2.i == u else q2.i
                    if lo[v] == hi[v] == val:
                        raise Infeasible
                    if lo[v] <= val <= hi[v] and val not in excluded[v]:
                        excluded[v].add(val)
                        self._shrink_excluded(v, dom, excluded)
                        enqueue(v)
        # final emptiness check against excluded sets
        for v in range(self.n):
            if self._domain_size(lo[v], hi[v], excluded[v]) <= 0:
                raise Infeasible

    @staticmethod
    def _shrink_excluded(v: int, dom: list[list[int]],
                         excluded: list[set[int]]) -> None:
        lo, hi = dom
        while lo[v] in excluded[v]:
            lo[v] += 1
        while hi[v] in excluded[v]:
            hi[v] -= 1
        if lo[v] > hi[v]:
            raise Infeasible

    @staticmethod
    def _domain_size(lo: int, hi: int, excluded: set[int]) -> int:
        return (hi - lo + 1) - sum(1 for x in excluded if lo <= x <= hi)

    # ------------------------------------------------------------------ #
    # Search
    # ------------------------------------------------------------------ #

    def feasible(self, extra_unary: Unary | None = None) -> bool:
        try:
            (lo, hi), excluded = self.initial_domains()
        except Infeasible:
            return False
        if extra_unary is not None:
            lo[extra_unary.var] = max(lo[extra_unary.var], extra_unary.lo)
            hi[extra_unary.var] = min(hi[extra_unary.var], extra_unary.hi)
            if lo[extra_unary.var] > hi[extra_unary.var]:
                return False
        try:
            self.propagate([lo, hi], excluded, list(range(self.n)))
            return bool(self._dfs([lo, hi], excluded))
        except Infeasible:
            return False

    def witness(self, extra_unary: Unary | None = None) -> dict[int, int] | None:
        """Return a full lap assignment {var: k} or None."""
        try:
            (lo, hi), excluded = self.initial_domains()
        except Infeasible:
            return None
        if extra_unary is not None:
            lo[extra_unary.var] = max(lo[extra_unary.var], extra_unary.lo)
            hi[extra_unary.var] = min(hi[extra_unary.var], extra_unary.hi)
            if lo[extra_unary.var] > hi[extra_unary.var]:
                return None
        try:
            self.propagate([lo, hi], excluded, list(range(self.n)))
        except Infeasible:
            return None
        return self._dfs([lo, hi], excluded, collect=True)

    # Above this many admissible values a variable is explored by interval
    # bisection rather than per-value enumeration (logarithmic in domain size,
    # which matters when unary bounds reach 10**12 scale).
    VALUE_ENUM_LIMIT = 32

    def _dfs(self, dom: list[list[int]], excluded: list[set[int]],
             collect: bool = False):
        lo, hi = dom
        # Branch on any unassigned variable that participates in a
        # disequality: even with no pinned partner yet, the all-different
        # coupling can only be resolved by assigning values and propagating.
        # Smallest surviving domain is chosen first.
        candidate = None
        for v in range(self.n):
            if lo[v] == hi[v] or not self.neq_of[v]:
                continue
            size = self._domain_size(lo[v], hi[v], excluded[v])
            if candidate is None or size < candidate[0]:
                candidate = (size, v)
        if candidate is None:
            # No unassigned disequality variable: interval domains admit a
            # value and pure difference constraints are simultaneously
            # satisfiable; pinned-variable pruning already handled conflicts.
            assignment = {v: lo[v] for v in range(self.n)}
            return assignment if collect else True

        v = candidate[1]
        holes = sorted(x for x in excluded[v] if lo[v] <= x <= hi[v])
        free_pieces: list[tuple[int, int]] = []
        cursor = lo[v]
        for h in holes:
            if cursor <= h - 1:
                free_pieces.append((cursor, h - 1))
            cursor = h + 1
        if cursor <= hi[v]:
            free_pieces.append((cursor, hi[v]))

        # Each free piece is explored: small pieces by assigning each value,
        # large pieces by recursive bisection into two sub-intervals.  This is
        # complete for any domain size and logarithmic in the numeric bounds.
        for piece_lo, piece_hi in free_pieces:
            res = self._explore_piece(v, piece_lo, piece_hi, dom,
                                      excluded, collect)
            if res is not None:
                return res
        return None

    def _explore_piece(self, v: int, piece_lo: int, piece_hi: int,
                       dom: list[list[int]], excluded: list[set[int]],
                       collect: bool):
        lo, hi = dom
        width = piece_hi - piece_lo + 1
        if width <= self.VALUE_ENUM_LIMIT:
            candidates = list(range(piece_lo, piece_hi + 1))
        else:
            mid = (piece_lo + piece_hi) // 2
            candidates = [mid, mid + 1]      # bisection boundary values
        for val in candidates:
            snap_lo, snap_hi = lo[:], hi[:]
            snap_ex = [set(s) for s in excluded]
            lo[v] = hi[v] = val
            try:
                self.propagate([lo, hi], excluded, [v])
                res = self._dfs([lo, hi], excluded, collect)
            except Infeasible:
                res = None
            if res is not None:
                return res
            lo[:], hi[:] = snap_lo, snap_hi
            excluded[:] = snap_ex
        # For a bisected large piece, also recurse into both halves as
        # interval restrictions (not singletons), preserving completeness.
        if width > self.VALUE_ENUM_LIMIT:
            mid = (piece_lo + piece_hi) // 2
            for sub_lo, sub_hi in ((piece_lo, mid), (mid + 1, piece_hi)):
                snap_lo, snap_hi = lo[:], hi[:]
                snap_ex = [set(s) for s in excluded]
                lo[v], hi[v] = sub_lo, sub_hi
                try:
                    self.propagate([lo, hi], excluded, [v])
                    res = self._dfs([lo, hi], excluded, collect)
                except Infeasible:
                    res = None
                if res is not None:
                    return res
                lo[:], hi[:] = snap_lo, snap_hi
                excluded[:] = snap_ex
        return None

    # ------------------------------------------------------------------ #
    # Extremal lap values with full-assignment witnesses
    # ------------------------------------------------------------------ #

    def extremum(self, var: int, minimize: bool
                 ) -> tuple[int, dict[int, int]] | None:
        """Smallest/largest feasible k for ``var`` with a witnessing assignment."""
        (lo0, hi0), _ = self.initial_domains()
        if not self.feasible():
            return None
        lo, hi = lo0[var], hi0[var]
        # binary search the boundary of feasibility
        if minimize:
            while lo < hi:
                mid = (lo + hi) // 2
                if self.feasible(Unary(var, lo0[var], mid, ())):
                    hi = mid
                else:
                    lo = mid + 1
            target = lo
        else:
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if self.feasible(Unary(var, mid, hi0[var], ())):
                    lo = mid
                else:
                    hi = mid - 1
            target = lo
        assignment = self.witness(Unary(var, target, target, ()))
        assert assignment is not None and assignment[var] == target
        return target, assignment

    # ------------------------------------------------------------------ #
    # Sequence helpers
    # ------------------------------------------------------------------ #

    def to_sequences(self, assignment: dict[int, int]) -> dict[str, int]:
        return {self.comp.var_event[v]:
                self.comp.residues[v] + self.M * assignment[v]
                for v in range(self.n)}


# --------------------------------------------------------------------------- #
# Reference oracle: exhaustive enumeration over small lap domains.
# Used only by the unittest cross-check, never by the production path.
# --------------------------------------------------------------------------- #

def brute_force_solutions(comp: Component, cap: int = 4096,
                          k_ceiling: int | None = None
                          ) -> list[dict[str, int]]:
    """Enumerate every full lap assignment with k values inside a small range.

    ``k_ceiling`` bounds laps explicitly for tests that removed constraints
    (for example a repair pause), where the component's own unary upper bound
    can be the symbolic 10**18//M sentinel.  Production code never calls this.
    """
    n = len(comp.var_event)
    lo = [0] * n
    hi = [10**18] * n
    for u in comp.unary:
        lo[u.var] = max(lo[u.var], u.lo)
        hi[u.var] = min(hi[u.var], u.hi)
    if k_ceiling is not None:
        hi = [min(h, k_ceiling) for h in hi]
    if any(h > cap for h in hi):
        raise ValueError("brute_force_solutions only supports small bounded domains")
    domains = [list(range(lo[v], hi[v] + 1)) for v in range(n)]
    out: list[dict[str, int]] = []
    for combo in itertools.product(*domains):
        ok = True
        for e in comp.edges:
            d = combo[e.j] - combo[e.i]
            if not (e.lo <= d <= e.hi):
                ok = False
                break
        if not ok:
            continue
        for q in comp.neqs:
            if combo[q.i] == combo[q.j]:
                ok = False
                break
        if ok:
            out.append({comp.var_event[v]:
                        comp.residues[v] + comp.modulus * combo[v]
                        for v in range(n)})
    return out
