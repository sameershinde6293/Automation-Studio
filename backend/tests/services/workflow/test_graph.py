"""Tests for the pure graph algorithms backing the workflow engine."""

import pytest

from app.services.workflow.graph import (
    build_adjacency,
    descendants,
    execution_layers,
    find_cycles,
    topological_order,
    validate_graph,
)


class TestBuildAdjacency:
    def test_simple_chain(self):
        deps, dependents = build_adjacency([1, 2, 3], [(1, 2), (2, 3)])
        assert deps == {1: set(), 2: {1}, 3: {2}}
        assert dependents == {1: {2}, 2: {3}, 3: set()}

    def test_ignores_edges_to_unknown_nodes(self):
        deps, _ = build_adjacency([1, 2], [(1, 2), (2, 99), (77, 1)])
        assert deps == {1: set(), 2: {1}}

    def test_fan_out_and_fan_in(self):
        deps, dependents = build_adjacency([1, 2, 3, 4], [(1, 2), (1, 3), (2, 4), (3, 4)])
        assert deps[4] == {2, 3}
        assert dependents[1] == {2, 3}

    def test_self_loop_recorded(self):
        deps, _ = build_adjacency([1], [(1, 1)])
        assert deps[1] == {1}

    def test_empty_graph(self):
        deps, dependents = build_adjacency([], [])
        assert deps == {} and dependents == {}


class TestFindCycles:
    def test_acyclic_returns_empty(self):
        assert find_cycles([1, 2, 3], [(1, 2), (2, 3)]) == []

    def test_detects_two_node_cycle(self):
        cycles = find_cycles([1, 2], [(1, 2), (2, 1)])
        assert len(cycles) == 1
        assert set(cycles[0]) == {1, 2}

    def test_detects_three_node_cycle(self):
        cycles = find_cycles([1, 2, 3], [(1, 2), (2, 3), (3, 1)])
        assert len(cycles) == 1
        assert set(cycles[0]) == {1, 2, 3}

    def test_detects_self_loop(self):
        cycles = find_cycles([1], [(1, 1)])
        assert cycles == [[1]]

    def test_diamond_is_not_a_cycle(self):
        assert find_cycles([1, 2, 3, 4], [(1, 2), (1, 3), (2, 4), (3, 4)]) == []

    def test_deep_chain_does_not_recurse(self):
        """Iterative DFS must survive graphs deeper than the recursion limit."""
        n = 3000
        ids = list(range(n))
        edges = [(i, i + 1) for i in range(n - 1)]
        assert find_cycles(ids, edges) == []

    def test_cycle_plus_acyclic_component(self):
        cycles = find_cycles([1, 2, 3, 4], [(1, 2), (2, 1), (3, 4)])
        assert len(cycles) == 1


class TestTopologicalOrder:
    def test_linear_order(self):
        assert topological_order([1, 2, 3], [(1, 2), (2, 3)]) == [1, 2, 3]

    def test_respects_dependencies(self):
        order = topological_order([1, 2, 3, 4], [(1, 2), (1, 3), (2, 4), (3, 4)])
        assert order.index(1) < order.index(2) < order.index(4)
        assert order.index(3) < order.index(4)

    def test_raises_on_cycle(self):
        with pytest.raises(ValueError, match="cycle"):
            topological_order([1, 2], [(1, 2), (2, 1)])

    def test_isolated_nodes(self):
        assert sorted(topological_order([1, 2, 3], [])) == [1, 2, 3]


class TestValidateGraph:
    def test_valid_dag(self):
        result = validate_graph([1, 2, 3], [(1, 2), (2, 3)])
        assert result.is_valid
        assert result.errors == []

    def test_empty_graph_is_invalid(self):
        result = validate_graph([], [])
        assert not result.is_valid
        assert "no nodes" in result.errors[0]

    def test_cycle_reported(self):
        result = validate_graph([1, 2], [(1, 2), (2, 1)])
        assert not result.is_valid
        assert any("Cycle detected" in e for e in result.errors)
        assert result.cycles

    def test_self_edge_reported(self):
        result = validate_graph([1, 2], [(1, 1)])
        assert not result.is_valid
        assert any("self-referencing" in e for e in result.errors)

    def test_unknown_edge_endpoint(self):
        result = validate_graph([1, 2], [(1, 99)])
        assert not result.is_valid
        assert any("unknown target" in e for e in result.errors)

    def test_max_nodes_enforced(self):
        result = validate_graph([1, 2, 3], [], max_nodes=2)
        assert not result.is_valid
        assert any("maximum" in e for e in result.errors)

    def test_orphan_produces_warning_not_error(self):
        result = validate_graph([1, 2, 3], [(1, 2)])
        assert result.is_valid
        assert result.orphans == [3]
        assert result.warnings

    def test_duplicate_node_ids(self):
        result = validate_graph([1, 1, 2], [])
        assert not result.is_valid
        assert any("Duplicate" in e for e in result.errors)

    def test_raise_if_invalid_raises(self):
        from app.core.errors import ValidationError

        result = validate_graph([1, 2], [(1, 2), (2, 1)])
        with pytest.raises(ValidationError):
            result.raise_if_invalid()

    def test_raise_if_invalid_noop_when_valid(self):
        validate_graph([1], []).raise_if_invalid()  # must not raise


class TestExecutionLayers:
    def test_linear_chain_is_one_node_per_layer(self):
        assert execution_layers([1, 2, 3], [(1, 2), (2, 3)]) == [[1], [2], [3]]

    def test_parallel_nodes_share_a_layer(self):
        layers = execution_layers([1, 2, 3, 4], [(1, 2), (1, 3), (2, 4), (3, 4)])
        assert layers[0] == [1]
        assert sorted(layers[1]) == [2, 3]
        assert layers[2] == [4]

    def test_all_independent_nodes_in_one_layer(self):
        layers = execution_layers([1, 2, 3], [])
        assert len(layers) == 1
        assert sorted(layers[0]) == [1, 2, 3]

    def test_raises_on_cycle(self):
        with pytest.raises(ValueError):
            execution_layers([1, 2], [(1, 2), (2, 1)])


class TestDescendants:
    def test_collects_transitive_children(self):
        assert descendants(1, [(1, 2), (2, 3), (3, 4)]) == {2, 3, 4}

    def test_leaf_has_no_descendants(self):
        assert descendants(3, [(1, 2), (2, 3)]) == set()

    def test_branching(self):
        assert descendants(1, [(1, 2), (1, 3), (3, 4)]) == {2, 3, 4}

    def test_handles_cycles_without_hanging(self):
        assert descendants(1, [(1, 2), (2, 1)]) == {2}
