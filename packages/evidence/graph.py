"""Stage-8 Evidence Correlation Graph (C07, draft) — relational tables, no graph DB.

Nodes: Run, Step, Event, Evidence, Assertion, Hypothesis, ProbeExecution, Source.
Edges (v1.1 §阶段8): parent_child, emitted_by, derived_from,
correlated_by_trace_id, correlated_by_request_id, same_execution_context,
temporal, supports, contradicts, evaluates. ``direct_causal`` is deliberately
NOT in the auto-edge rules: temporal proximity / same page / similar logs never
create a causal edge. It requires an explicit, trusted direct-causal basis.

Every edge carries evidence: source field(s), rule id, rule version. Unknown
correlation values are preserved as UNKNOWN and NEVER become deterministic edges.
Source independence: derived evidence sharing a source with another is not an
independent corroboration (correlation != causation, derived != raw).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, String, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from packages.core.errors import ConflictError, ValidationError

RULE_VERSION = "1.0.0"

NODE_TYPES = frozenset(
    {
        "Run",
        "Step",
        "Event",
        "Evidence",
        "Assertion",
        "Hypothesis",
        "ProbeExecution",
        "Source",
    }
)
AUTO_EDGE_TYPES = frozenset(
    {
        "parent_child",
        "emitted_by",
        "derived_from",
        "correlated_by_trace_id",
        "correlated_by_request_id",
        "same_execution_context",
        "temporal",
        "supports",
        "contradicts",
        "evaluates",
    }
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


class GraphBase(DeclarativeBase):
    pass


class NodeRow(GraphBase):
    __tablename__ = "gr_node"

    node_id: Mapped[str] = mapped_column(String, primary_key=True)
    node_type: Mapped[str] = mapped_column(String, nullable=False, index=True)
    source_ref: Mapped[str] = mapped_column(String, default="", nullable=False)
    source_independence_group: Mapped[str] = mapped_column(String, default="", nullable=False)
    properties: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[str] = mapped_column(String, default=_now, nullable=False)


class EdgeRow(GraphBase):
    __tablename__ = "gr_edge"

    edge_id: Mapped[str] = mapped_column(String, primary_key=True)
    src: Mapped[str] = mapped_column(String, nullable=False, index=True)
    dst: Mapped[str] = mapped_column(String, nullable=False, index=True)
    relation: Mapped[str] = mapped_column(String, nullable=False, index=True)
    basis_fields: Mapped[str] = mapped_column(String, default="", nullable=False)
    rule_id: Mapped[str] = mapped_column(String, default="", nullable=False)
    rule_version: Mapped[str] = mapped_column(String, default=RULE_VERSION, nullable=False)
    created_at: Mapped[str] = mapped_column(String, default=_now, nullable=False)


@dataclass(frozen=True)
class GraphNode:
    node_id: str
    node_type: str
    source_ref: str = ""
    independence_group: str = ""
    properties: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.node_type not in NODE_TYPES:
            raise ValidationError(f"invalid node type: {self.node_type}")


@dataclass(frozen=True)
class GraphEdge:
    edge_id: str
    src: str
    dst: str
    relation: str
    basis_fields: str = ""
    rule_id: str = ""
    rule_version: str = RULE_VERSION

    def __post_init__(self) -> None:
        if self.relation not in AUTO_EDGE_TYPES and self.relation != "direct_causal":
            raise ValidationError(f"invalid edge relation: {self.relation}")


class CorrelationGraph:
    """Append-only graph store + correlation/independence/conflict analysis."""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._sessions = session_factory

    def add_node(self, node: GraphNode) -> dict[str, Any]:
        with self._sessions() as s:
            if s.get(NodeRow, node.node_id):
                raise ConflictError(f"node exists: {node.node_id}")
            s.add(
                NodeRow(
                    node_id=node.node_id,
                    node_type=node.node_type,
                    source_ref=node.source_ref,
                    source_independence_group=node.independence_group,
                    properties=dict(node.properties or {}),
                )
            )
            s.commit()
            return {"node_id": node.node_id, "node_type": node.node_type}

    def add_edge(self, edge: GraphEdge) -> dict[str, Any]:
        if edge.relation == "direct_causal" and not edge.basis_fields:
            raise ValidationError("direct_causal requires explicit trusted basis_fields")
        with self._sessions() as s:
            if s.get(EdgeRow, edge.edge_id):
                raise ConflictError(f"edge exists: {edge.edge_id}")
            if not s.get(NodeRow, edge.src) or not s.get(NodeRow, edge.dst):
                raise ValidationError(f"edge endpoints unknown: {edge.src} -> {edge.dst}")
            s.add(
                EdgeRow(
                    edge_id=edge.edge_id,
                    src=edge.src,
                    dst=edge.dst,
                    relation=edge.relation,
                    basis_fields=edge.basis_fields,
                    rule_id=edge.rule_id,
                    rule_version=edge.rule_version,
                )
            )
            s.commit()
            return asdict(edge)

    def nodes(self, node_type: str | None = None) -> list[dict[str, Any]]:
        with self._sessions() as s:
            stmt = select(NodeRow)
            if node_type is not None:
                stmt = stmt.where(NodeRow.node_type == node_type)
            rows = s.execute(stmt.order_by(NodeRow.created_at)).scalars().all()
            return [
                {
                    "node_id": r.node_id,
                    "node_type": r.node_type,
                    "source_ref": r.source_ref,
                    "independence_group": r.source_independence_group,
                    "properties": r.properties,
                }
                for r in rows
            ]

    def edges(self, relation: str | None = None) -> list[dict[str, Any]]:
        with self._sessions() as s:
            stmt = select(EdgeRow)
            if relation is not None:
                stmt = stmt.where(EdgeRow.relation == relation)
            rows = s.execute(stmt.order_by(EdgeRow.created_at)).scalars().all()
            return [
                {
                    "edge_id": r.edge_id,
                    "src": r.src,
                    "dst": r.dst,
                    "relation": r.relation,
                    "basis_fields": r.basis_fields,
                    "rule_id": r.rule_id,
                    "rule_version": r.rule_version,
                }
                for r in rows
            ]

    def neighbors(self, node_id: str, relation: str | None = None) -> list[dict[str, Any]]:
        with self._sessions() as s:
            stmt = select(EdgeRow).where((EdgeRow.src == node_id) | (EdgeRow.dst == node_id))
            if relation is not None:
                stmt = stmt.where(EdgeRow.relation == relation)
            rows = s.execute(stmt).scalars().all()
            return [
                {
                    "edge_id": r.edge_id,
                    "src": r.src,
                    "dst": r.dst,
                    "relation": r.relation,
                    "basis_fields": r.basis_fields,
                    "rule_id": r.rule_id,
                    "other": r.dst if r.src == node_id else r.src,
                }
                for r in rows
            ]

    def subgraph(self, root_id: str, depth: int = 2) -> dict[str, Any]:
        """BFS subgraph for visualization; bounded depth prevents runaway queries."""
        if depth < 0 or depth > 6:
            raise ValidationError("depth must be 0..6")
        visited: set[str] = {root_id}
        frontier = [root_id]
        for _ in range(depth):
            nxt: list[str] = []
            for nid in frontier:
                for e in self.neighbors(nid):
                    other = e["other"]
                    if other not in visited:
                        visited.add(other)
                        nxt.append(other)
            frontier = nxt
        node_index = {n["node_id"]: n for n in self.nodes()}
        return {
            "root": root_id,
            "nodes": [node_index[n] for n in visited if n in node_index],
            "edges": [e for e in self.edges() if e["src"] in visited and e["dst"] in visited],
        }

    # ---- correlation rules -------------------------------------------------

    def correlate_events(self, events: list[dict[str, Any]]) -> list[GraphEdge]:
        """Build edges ONLY from validated context_refs; unknown refs -> no edge.

        No cross-run causal inference. Trace/request/same-context edges require
        a non-empty validated value on BOTH endpoints.
        """
        edges: list[GraphEdge] = []
        for i, e in enumerate(events):
            refs = e.get("context_refs") or {}
            for key, relation in (
                ("trace_id", "correlated_by_trace_id"),
                ("request_id", "correlated_by_request_id"),
            ):
                value = refs.get(key)
                if not value or value == "unknown":
                    continue  # unknown correlation preserved as no edge
                for j, other in enumerate(events):
                    if i >= j:
                        continue
                    other_refs = other.get("context_refs") or {}
                    if other_refs.get(key) == value:
                        edges.append(
                            GraphEdge(
                                edge_id=f"edge_{relation}_{i}_{j}",
                                src=str(e.get("event_id")),
                                dst=str(other.get("event_id")),
                                relation=relation,
                                basis_fields=f"context_refs.{key}",
                                rule_id=f"R-CORR-{key}",
                            )
                        )
        return edges

    def independent_corroborations(self, evidence_nodes: list[dict[str, Any]]) -> dict[str, Any]:
        """Group evidence by independence source; same-source derived items are
        NOT counted as multiple independent corroborations."""
        groups: dict[str, list[str]] = {}
        for n in evidence_nodes:
            group = n.get("independence_group") or n.get("properties", {}).get("source", "unknown")
            groups.setdefault(str(group), []).append(n["node_id"])
        independent_sources = len([g for g, ids in groups.items() if ids])
        return {
            "groups": groups,
            "independent_source_count": independent_sources,
            "note": (
                "derived evidence from the same source counts as ONE independent "
                "source; correlation is not causation"
            ),
        }

    def conflicts(self, hypothesis_id: str) -> dict[str, Any]:
        """Both supports and contradicts edges are preserved and displayed."""
        supports = [e for e in self.neighbors(hypothesis_id, "supports")]
        contradicts = [e for e in self.neighbors(hypothesis_id, "contradicts")]
        return {
            "hypothesis_id": hypothesis_id,
            "supports": supports,
            "contradicts": contradicts,
            "has_conflict": bool(supports and contradicts),
        }


def create_graph_tables(engine: Any) -> None:
    GraphBase.metadata.create_all(engine, checkfirst=True)


def make_in_memory_graph() -> tuple[CorrelationGraph, Callable[[], Session]]:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    GraphBase.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    return CorrelationGraph(factory), factory
