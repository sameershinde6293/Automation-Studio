"""Pure graph algorithms for the workflow engine.

Kept free of ORM/IO dependencies so they are cheap to unit-test and reusable by
both the engine and the API validation layer.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Dict, Hashable, Iterable, List, Sequence, Set, Tuple

NodeId = Hashable


@dataclass
class GraphValidation:
    """Result of validating a workflow graph."""

    is_valid: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    cycles: List[List[NodeId]] = field(default_factory=list)
    orphans: List[NodeId] = field(default_factory=list)

    def raise_if_invalid(self) -> None:
        from app.core.errors import ValidationError

        if not self.is_valid:
            raise ValidationError(
                "Workflow graph is invalid.",
                details={"errors": self.errors, "cycles": [list(c) for c in self.cycles]},
            )


def build_adjacency(
    node_ids: Iterable[NodeId], edges: Sequence[Tuple[NodeId, NodeId]]
) -> Tuple[Dict[NodeId, Set[NodeId]], Dict[NodeId, Set[NodeId]]]:
    """Return ``(dependencies, dependents)`` maps.

    ``dependencies[n]`` = set of nodes that must finish before ``n`` runs.
    ``dependents[n]``   = set of nodes waiting on ``n``.
    """
    ids = list(node_ids)
    dependencies: Dict[NodeId, Set[NodeId]] = {n: set() for n in ids}
    dependents: Dict[NodeId, Set[NodeId]] = {n: set() for n in ids}
    known = set(ids)
    for source, target in edges:
        if source not in known or target not in known:
            continue
        if source == target:
            # Self-loop: recorded as a dependency so cycle detection reports it.
            dependencies[target].add(source)
            dependents[source].add(target)
            continue
        dependencies[target].add(source)
        dependents[source].add(target)
    return dependencies, dependents


def find_cycles(
    node_ids: Iterable[NodeId], edges: Sequence[Tuple[NodeId, NodeId]]
) -> List[List[NodeId]]:
    """Return every elementary cycle found via iterative DFS.

    Iterative (not recursive) so deep graphs cannot blow the Python stack.
    """
    ids = list(node_ids)
    known = set(ids)
    successors: Dict[NodeId, List[NodeId]] = defaultdict(list)
    for source, target in edges:
        if source in known and target in known:
            successors[source].append(target)

    WHITE, GREY, BLACK = 0, 1, 2
    colour: Dict[NodeId, int] = {n: WHITE for n in ids}
    cycles: List[List[NodeId]] = []
    seen_signatures: Set[Tuple[NodeId, ...]] = set()

    for root in ids:
        if colour[root] != WHITE:
            continue
        stack: List[Tuple[NodeId, int]] = [(root, 0)]
        path: List[NodeId] = []
        on_path: Set[NodeId] = set()
        colour[root] = GREY
        path.append(root)
        on_path.add(root)

        while stack:
            node, index = stack[-1]
            children = successors.get(node, ())
            if index < len(children):
                stack[-1] = (node, index + 1)
                child = children[index]
                if child in on_path:
                    start = path.index(child)
                    cycle = path[start:]
                    signature = _canonical_cycle(cycle)
                    if signature not in seen_signatures:
                        seen_signatures.add(signature)
                        cycles.append(list(cycle))
                elif colour.get(child, WHITE) == WHITE:
                    colour[child] = GREY
                    path.append(child)
                    on_path.add(child)
                    stack.append((child, 0))
            else:
                colour[node] = BLACK
                stack.pop()
                if path and path[-1] == node:
                    path.pop()
                    on_path.discard(node)
    return cycles


def _canonical_cycle(cycle: Sequence[NodeId]) -> Tuple[NodeId, ...]:
    """Rotation-invariant signature so A->B->A and B->A->B dedupe."""
    if not cycle:
        return ()
    items = [str(c) for c in cycle]
    min_index = items.index(min(items))
    rotated = list(cycle[min_index:]) + list(cycle[:min_index])
    return tuple(rotated)


def topological_order(
    node_ids: Iterable[NodeId], edges: Sequence[Tuple[NodeId, NodeId]]
) -> List[NodeId]:
    """Kahn's algorithm. Raises ``ValueError`` if the graph contains a cycle."""
    ids = list(node_ids)
    dependencies, dependents = build_adjacency(ids, edges)
    indegree = {n: len(dependencies[n]) for n in ids}
    queue = deque(sorted((n for n in ids if indegree[n] == 0), key=str))
    order: List[NodeId] = []

    while queue:
        node = queue.popleft()
        order.append(node)
        for child in sorted(dependents[node], key=str):
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)

    if len(order) != len(ids):
        raise ValueError("Graph contains at least one cycle; no topological order exists.")
    return order


def validate_graph(
    node_ids: Iterable[NodeId],
    edges: Sequence[Tuple[NodeId, NodeId]],
    *,
    max_nodes: int = 1000,
) -> GraphValidation:
    """Validate a workflow DAG before execution or persistence."""
    ids = list(node_ids)
    errors: List[str] = []
    warnings: List[str] = []

    if not ids:
        errors.append("Workflow has no nodes.")
        return GraphValidation(is_valid=False, errors=errors)

    if len(ids) > max_nodes:
        errors.append(f"Workflow exceeds the maximum of {max_nodes} nodes ({len(ids)}).")

    if len(set(ids)) != len(ids):
        errors.append("Duplicate node ids detected.")

    known = set(ids)
    for source, target in edges:
        if source not in known:
            errors.append(f"Edge references unknown source node {source!r}.")
        if target not in known:
            errors.append(f"Edge references unknown target node {target!r}.")
        if source == target:
            errors.append(f"Node {source!r} has a self-referencing edge.")

    cycles = find_cycles(ids, edges)
    if cycles:
        for cycle in cycles:
            errors.append("Cycle detected: " + " -> ".join(str(c) for c in cycle + [cycle[0]]))

    dependencies, dependents = build_adjacency(ids, edges)
    orphans = [
        n for n in ids if not dependencies[n] and not dependents[n] and len(ids) > 1
    ]
    if orphans:
        warnings.append(
            "Disconnected node(s) will run independently: "
            + ", ".join(str(o) for o in orphans)
        )

    return GraphValidation(
        is_valid=not errors,
        errors=errors,
        warnings=warnings,
        cycles=cycles,
        orphans=orphans,
    )


def execution_layers(
    node_ids: Iterable[NodeId], edges: Sequence[Tuple[NodeId, NodeId]]
) -> List[List[NodeId]]:
    """Group nodes into parallel-executable layers (all deps in prior layers)."""
    ids = list(node_ids)
    dependencies, dependents = build_adjacency(ids, edges)
    indegree = {n: len(dependencies[n]) for n in ids}
    current = sorted((n for n in ids if indegree[n] == 0), key=str)
    layers: List[List[NodeId]] = []
    resolved = 0

    while current:
        layers.append(current)
        resolved += len(current)
        nxt: List[NodeId] = []
        for node in current:
            for child in dependents[node]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    nxt.append(child)
        current = sorted(nxt, key=str)

    if resolved != len(ids):
        raise ValueError("Graph contains at least one cycle; cannot compute layers.")
    return layers


def descendants(
    start: NodeId, edges: Sequence[Tuple[NodeId, NodeId]]
) -> Set[NodeId]:
    """All nodes reachable from ``start`` (used to skip a failed branch)."""
    successors: Dict[NodeId, List[NodeId]] = defaultdict(list)
    for source, target in edges:
        successors[source].append(target)

    seen: Set[NodeId] = set()
    queue = deque(successors.get(start, ()))
    while queue:
        node = queue.popleft()
        if node in seen or node == start:
            continue
        seen.add(node)
        queue.extend(successors.get(node, ()))
    return seen
