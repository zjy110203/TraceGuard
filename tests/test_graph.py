"""Stage-8 acceptance: Evidence Correlation Graph (relational, no graph DB).

Verifies:
  * all required node types and edge relations;
  * edges carry traceable basis (source field + rule + version);
  * deterministic correlation only from VALIDATED context_refs; unknown -> no edge;
  * no auto direct_causal from temporal proximity; direct_causal requires basis;
  * same-source derived evidence is not independent corroboration;
  * supports/contradicts both preserved for conflict display;
  * bounded subgraph query for visualization.
"""

from __future__ import annotations

import pytest

from packages.core.errors import ValidationError
from packages.evidence.graph import (
    NODE_TYPES,
    GraphEdge,
    GraphNode,
    make_in_memory_graph,
)


@pytest.fixture()
def graph():
    g, _factory = make_in_memory_graph()
    return g


def test_all_node_types_and_relations_declared() -> None:
    assert NODE_TYPES == {
        "Run",
        "Step",
        "Event",
        "Evidence",
        "Assertion",
        "Hypothesis",
        "ProbeExecution",
        "Source",
    }
    with pytest.raises(ValidationError):
        GraphNode("x", "NotANode")
    with pytest.raises(ValidationError):
        GraphEdge("e", "a", "b", "random_relation")


def test_nodes_and_edges_with_traceable_basis(graph) -> None:
    for nid in ("run1", "step1", "evd1", "hyp1"):
        graph.add_node(
            GraphNode(
                nid,
                {"run1": "Run", "step1": "Step", "evd1": "Evidence", "hyp1": "Hypothesis"}[nid],
                independence_group="browser",
            )
        )
    graph.add_edge(
        GraphEdge("e1", "run1", "step1", "parent_child", basis_fields="run_id", rule_id="R-STRUCT")
    )
    graph.add_edge(
        GraphEdge("e2", "evd1", "hyp1", "supports", basis_fields="evidence_refs", rule_id="R-SUP")
    )
    edges = {e["edge_id"]: e for e in graph.edges()}
    assert edges["e1"]["rule_id"] == "R-STRUCT"
    assert edges["e1"]["rule_version"] == "1.0.0"
    assert edges["e2"]["relation"] == "supports"
    assert graph.neighbors("hyp1", "supports")[0]["other"] == "evd1"


def test_unknown_node_endpoint_rejected(graph) -> None:
    with pytest.raises(ValidationError):
        graph.add_edge(GraphEdge("e-bad", "run1", "missing", "parent_child"))


def test_direct_causal_requires_explicit_trusted_basis(graph) -> None:
    graph.add_node(GraphNode("a", "Evidence"))
    graph.add_node(GraphNode("b", "Hypothesis"))
    with pytest.raises(ValidationError):
        graph.add_edge(GraphEdge("c-bad", "a", "b", "direct_causal"))  # no basis
    graph.add_edge(
        GraphEdge(
            "c-ok",
            "a",
            "b",
            "direct_causal",
            basis_fields="controlled_probe:P-ID:deterministic-rule",
            rule_id="R-DIRECT-CAUSAL",
        )
    )
    assert len(graph.edges("direct_causal")) == 1


def test_correlation_only_from_validated_context_refs() -> None:
    g, _factory = make_in_memory_graph()
    events = [
        {"event_id": "e1", "context_refs": {"trace_id": "t1", "request_id": ""}},
        {"event_id": "e2", "context_refs": {"trace_id": "t1", "request_id": "req-1"}},
        {"event_id": "e3", "context_refs": {"trace_id": "unknown", "request_id": "req-1"}},
        {"event_id": "e4", "context_refs": {"trace_id": "t9", "request_id": ""}},
    ]
    edges = g.correlate_events(events)
    trace_edges = [e for e in edges if e.relation == "correlated_by_trace_id"]
    request_edges = [e for e in edges if e.relation == "correlated_by_request_id"]
    # e1<->e2 by trace; e2<->e3 by request; 'unknown' trace never creates an edge
    assert len(trace_edges) == 1 and trace_edges[0].basis_fields == "context_refs.trace_id"
    assert len(request_edges) == 1
    assert all(e.src != "e3" or e.relation == "correlated_by_request_id" for e in edges)


def test_temporal_proximity_does_not_create_causal_edge() -> None:
    g, _factory = make_in_memory_graph()
    events = [
        {"event_id": "e1", "context_refs": {}, "occurred_at": "2026-01-01T00:00:00Z"},
        {"event_id": "e2", "context_refs": {}, "occurred_at": "2026-01-01T00:00:01Z"},
    ]
    assert g.correlate_events(events) == []  # no fabricated causal/correlation edge


def test_same_source_derived_not_independent(graph) -> None:
    evd = [
        {"node_id": "raw1", "independence_group": "browser"},
        {"node_id": "norm1", "independence_group": "browser"},  # derived from raw1
        {"node_id": "api1", "independence_group": "independent_api"},
    ]
    result = graph.independent_corroborations(evd)
    assert result["independent_source_count"] == 2  # browser (2 items) + api
    assert len(result["groups"]["browser"]) == 2
    assert "not independent" in result["note"] or "ONE independent" in result["note"]


def test_conflict_preserves_both_sides(graph) -> None:
    graph.add_node(GraphNode("hyp", "Hypothesis"))
    for nid in ("sup", "con"):
        graph.add_node(GraphNode(nid, "Evidence"))
    graph.add_edge(GraphEdge("s1", "sup", "hyp", "supports"))
    graph.add_edge(GraphEdge("c1", "con", "hyp", "contradicts"))
    result = graph.conflicts("hyp")
    assert result["has_conflict"] is True
    assert len(result["supports"]) == 1 and len(result["contradicts"]) == 1


def test_bounded_subgraph(graph) -> None:
    chain = ["n0", "n1", "n2", "n3", "n4"]
    for nid in chain:
        graph.add_node(GraphNode(nid, "Event"))
    for i in range(len(chain) - 1):
        graph.add_edge(GraphEdge(f"e{i}", chain[i], chain[i + 1], "temporal"))
    sub = graph.subgraph("n0", depth=2)
    assert {n["node_id"] for n in sub["nodes"]} == {"n0", "n1", "n2"}
    with pytest.raises(ValidationError):
        graph.subgraph("n0", depth=99)
