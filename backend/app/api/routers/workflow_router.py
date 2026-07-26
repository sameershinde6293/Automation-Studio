"""Workflow REST API: workflows, nodes, edges, executions and graph operations.

The V1.0 endpoints (`POST /api/workflows/`, `GET /api/workflows/`,
`POST /api/workflows/{execution_id}/run`) are preserved verbatim for backward
compatibility. Everything else is additive and is what the V1.1 drag-and-drop
editor persists against.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.domain.models.workflow import ExecutionStatus
from app.domain.repositories.workflow_repository import (
    EdgeCreate,
    NodeCreate,
    NodeUpdate,
    WorkflowCreate,
    WorkflowExecutionCreate,
    WorkflowUpdate,
    edge_repo,
    node_execution_repo,
    node_repo,
    workflow_execution_repo,
    workflow_repo,
)
from app.infrastructure.config.settings import settings
from app.infrastructure.database.database import get_db
from app.services.workflow.engine import workflow_engine
from app.services.workflow.executors import executor_registry
from app.services.workflow.graph import execution_layers, validate_graph

router = APIRouter(prefix="/workflows", tags=["Workflows"])


# --------------------------------------------------------------------------- #
# Request/response models
# --------------------------------------------------------------------------- #
class NodePayload(BaseModel):
    id: Optional[int] = None
    name: str
    node_type: str
    config: Optional[Dict[str, Any]] = None
    position_x: float = 0.0
    position_y: float = 0.0
    retry_policy: Optional[Dict[str, Any]] = None


class EdgePayload(BaseModel):
    source_id: int
    target_id: int
    label: Optional[str] = None


class GraphPayload(BaseModel):
    """Full-graph replacement used by the visual editor's save action."""

    nodes: List[NodePayload] = Field(default_factory=list)
    edges: List[EdgePayload] = Field(default_factory=list)


class RunRequest(BaseModel):
    trigger: Optional[str] = "manual"
    wait: bool = Field(
        False, description="Run synchronously and return the final result."
    )


def _require_workflow(db: Session, workflow_id: int):
    workflow = workflow_repo.get(db, workflow_id)
    if not workflow:
        raise NotFoundError(f"Workflow {workflow_id} not found.")
    return workflow


# --------------------------------------------------------------------------- #
# Workflow CRUD
# --------------------------------------------------------------------------- #
@router.post("/", summary="Create a workflow")
def create_workflow(workflow_in: WorkflowCreate, db: Session = Depends(get_db)):
    return workflow_repo.create(db, workflow_in)


@router.get("/", summary="List workflows")
def list_workflows(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    search: str = Query("", description="Case-insensitive name filter"),
    db: Session = Depends(get_db),
):
    if search:
        return workflow_repo.search_by_name(db, search, skip=skip, limit=limit)
    return workflow_repo.get_all(db, skip, limit)


@router.get("/{workflow_id}", summary="Get one workflow")
def get_workflow(workflow_id: int, db: Session = Depends(get_db)):
    return _require_workflow(db, workflow_id)


@router.put("/{workflow_id}", summary="Update a workflow")
def update_workflow(
    workflow_id: int, workflow_in: WorkflowUpdate, db: Session = Depends(get_db)
):
    workflow = _require_workflow(db, workflow_id)
    return workflow_repo.update(db, workflow, workflow_in)


@router.delete("/{workflow_id}", status_code=204, summary="Delete a workflow")
def delete_workflow(workflow_id: int, db: Session = Depends(get_db)) -> Response:
    _require_workflow(db, workflow_id)
    workflow_repo.delete(db, workflow_id)
    return Response(status_code=204)


# --------------------------------------------------------------------------- #
# Graph (nodes + edges)
# --------------------------------------------------------------------------- #
@router.get("/{workflow_id}/graph", summary="Fetch the full workflow graph")
def get_graph(workflow_id: int, db: Session = Depends(get_db)) -> Dict[str, Any]:
    workflow = _require_workflow(db, workflow_id)
    nodes = node_repo.get_by_workflow(db, workflow_id)
    edges = edge_repo.get_by_workflow(db, workflow_id)
    return {
        "workflow": {
            "id": workflow.id,
            "name": workflow.name,
            "description": workflow.description,
            "version": workflow.version,
        },
        "nodes": [
            {
                "id": n.id,
                "name": n.name,
                "node_type": n.node_type,
                "config": n.config or {},
                "position_x": n.position_x,
                "position_y": n.position_y,
                "retry_policy": n.retry_policy,
            }
            for n in nodes
        ],
        "edges": [
            {"id": e.id, "source_id": e.source_id, "target_id": e.target_id, "label": e.label}
            for e in edges
        ],
    }


@router.put("/{workflow_id}/graph", summary="Replace the full workflow graph")
def replace_graph(
    workflow_id: int, payload: GraphPayload, db: Session = Depends(get_db)
) -> Dict[str, Any]:
    """Atomically replace nodes and edges.

    Node payloads may carry a **client-side** ``id``; edges reference those same
    ids. The server remaps them to the persisted primary keys, so the editor can
    create nodes and edges in one round trip.
    """
    _require_workflow(db, workflow_id)

    if len(payload.nodes) > settings.WORKFLOW_MAX_NODES:
        raise ValidationError(
            f"Workflow exceeds the maximum of {settings.WORKFLOW_MAX_NODES} nodes."
        )

    unknown = sorted(
        {n.node_type for n in payload.nodes if not executor_registry.has(n.node_type)}
    )
    if unknown:
        raise ValidationError(
            "Unknown node type(s).",
            details={"node_types": unknown, "available": sorted(executor_registry.executors)},
        )

    client_ids = [n.id for n in payload.nodes if n.id is not None]
    validation = validate_graph(
        client_ids or [i for i, _ in enumerate(payload.nodes)],
        [(e.source_id, e.target_id) for e in payload.edges] if client_ids else [],
        max_nodes=settings.WORKFLOW_MAX_NODES,
    ) if payload.nodes else None
    if validation is not None and not validation.is_valid:
        validation.raise_if_invalid()

    # Clear existing graph, then rebuild.
    edge_repo.delete_by_workflow(db, workflow_id)
    node_repo.delete_by_workflow(db, workflow_id)

    id_map: Dict[int, int] = {}
    created_nodes = []
    for index, node in enumerate(payload.nodes):
        db_node = node_repo.create(
            db,
            NodeCreate(
                workflow_id=workflow_id,
                name=node.name,
                node_type=node.node_type,
                config=node.config,
                position_x=node.position_x,
                position_y=node.position_y,
                retry_policy=node.retry_policy,
            ),
        )
        created_nodes.append(db_node)
        id_map[node.id if node.id is not None else index] = db_node.id

    created_edges = []
    for edge in payload.edges:
        source = id_map.get(edge.source_id, edge.source_id)
        target = id_map.get(edge.target_id, edge.target_id)
        if source not in id_map.values() or target not in id_map.values():
            raise ValidationError(
                "Edge references a node that is not part of this graph.",
                details={"source_id": edge.source_id, "target_id": edge.target_id},
            )
        created_edges.append(
            edge_repo.create(
                db,
                EdgeCreate(
                    workflow_id=workflow_id,
                    source_id=source,
                    target_id=target,
                    label=edge.label,
                ),
            )
        )

    return {
        "workflow_id": workflow_id,
        "node_count": len(created_nodes),
        "edge_count": len(created_edges),
        "id_map": {str(k): v for k, v in id_map.items()},
    }


@router.post("/{workflow_id}/validate", summary="Validate the workflow graph")
def validate_workflow(workflow_id: int, db: Session = Depends(get_db)) -> Dict[str, Any]:
    _require_workflow(db, workflow_id)
    nodes = node_repo.get_by_workflow(db, workflow_id)
    edges = edge_repo.get_by_workflow(db, workflow_id)
    node_ids = [n.id for n in nodes]
    pairs = [(e.source_id, e.target_id) for e in edges]

    result = validate_graph(node_ids, pairs, max_nodes=settings.WORKFLOW_MAX_NODES)
    layers: List[List[int]] = []
    if result.is_valid:
        try:
            layers = execution_layers(node_ids, pairs)
        except ValueError:
            layers = []

    unknown = sorted({n.node_type for n in nodes if not executor_registry.has(n.node_type)})
    errors = list(result.errors)
    if unknown:
        errors.append(f"Unknown node type(s): {', '.join(unknown)}")

    return {
        "is_valid": result.is_valid and not unknown,
        "errors": errors,
        "warnings": result.warnings,
        "cycles": result.cycles,
        "layers": layers,
        "node_count": len(nodes),
        "edge_count": len(edges),
    }


# --------------------------------------------------------------------------- #
# Node CRUD
# --------------------------------------------------------------------------- #
@router.get("/{workflow_id}/nodes", summary="List nodes")
def list_nodes(workflow_id: int, db: Session = Depends(get_db)):
    _require_workflow(db, workflow_id)
    return node_repo.get_by_workflow(db, workflow_id)


@router.post("/{workflow_id}/nodes", summary="Create a node")
def create_node(workflow_id: int, node: NodePayload, db: Session = Depends(get_db)):
    _require_workflow(db, workflow_id)
    if not executor_registry.has(node.node_type):
        raise ValidationError(
            f"Unknown node type {node.node_type!r}.",
            details={"available": sorted(executor_registry.executors)},
        )
    return node_repo.create(
        db,
        NodeCreate(
            workflow_id=workflow_id,
            name=node.name,
            node_type=node.node_type,
            config=node.config,
            position_x=node.position_x,
            position_y=node.position_y,
            retry_policy=node.retry_policy,
        ),
    )


@router.put("/{workflow_id}/nodes/{node_id}", summary="Update a node")
def update_node(
    workflow_id: int, node_id: int, payload: NodeUpdate, db: Session = Depends(get_db)
):
    _require_workflow(db, workflow_id)
    node = node_repo.get(db, node_id)
    if not node or node.workflow_id != workflow_id:
        raise NotFoundError(f"Node {node_id} not found in workflow {workflow_id}.")
    if payload.node_type and not executor_registry.has(payload.node_type):
        raise ValidationError(f"Unknown node type {payload.node_type!r}.")
    return node_repo.update(db, node, payload)


@router.delete("/{workflow_id}/nodes/{node_id}", status_code=204, summary="Delete a node")
def delete_node(workflow_id: int, node_id: int, db: Session = Depends(get_db)) -> Response:
    _require_workflow(db, workflow_id)
    node = node_repo.get(db, node_id)
    if not node or node.workflow_id != workflow_id:
        raise NotFoundError(f"Node {node_id} not found in workflow {workflow_id}.")
    edge_repo.delete_by_node(db, node_id)
    node_repo.delete(db, node_id)
    return Response(status_code=204)


# --------------------------------------------------------------------------- #
# Edge CRUD
# --------------------------------------------------------------------------- #
@router.get("/{workflow_id}/edges", summary="List edges")
def list_edges(workflow_id: int, db: Session = Depends(get_db)):
    _require_workflow(db, workflow_id)
    return edge_repo.get_by_workflow(db, workflow_id)


@router.post("/{workflow_id}/edges", summary="Create an edge")
def create_edge(workflow_id: int, edge: EdgePayload, db: Session = Depends(get_db)):
    _require_workflow(db, workflow_id)
    nodes = {n.id for n in node_repo.get_by_workflow(db, workflow_id)}
    if edge.source_id not in nodes or edge.target_id not in nodes:
        raise ValidationError(
            "Both source and target must be nodes in this workflow.",
            details={"source_id": edge.source_id, "target_id": edge.target_id},
        )
    if edge.source_id == edge.target_id:
        raise ValidationError("A node cannot connect to itself.")

    existing = [(e.source_id, e.target_id) for e in edge_repo.get_by_workflow(db, workflow_id)]
    if (edge.source_id, edge.target_id) in existing:
        raise ConflictError("This edge already exists.")

    candidate = existing + [(edge.source_id, edge.target_id)]
    result = validate_graph(list(nodes), candidate, max_nodes=settings.WORKFLOW_MAX_NODES)
    if result.cycles:
        raise ValidationError(
            "Adding this edge would create a cycle.",
            details={"cycles": [[str(c) for c in cycle] for cycle in result.cycles]},
        )

    return edge_repo.create(
        db,
        EdgeCreate(
            workflow_id=workflow_id,
            source_id=edge.source_id,
            target_id=edge.target_id,
            label=edge.label,
        ),
    )


@router.delete("/{workflow_id}/edges/{edge_id}", status_code=204, summary="Delete an edge")
def delete_edge(workflow_id: int, edge_id: int, db: Session = Depends(get_db)) -> Response:
    _require_workflow(db, workflow_id)
    edge = edge_repo.get(db, edge_id)
    if not edge or edge.workflow_id != workflow_id:
        raise NotFoundError(f"Edge {edge_id} not found in workflow {workflow_id}.")
    edge_repo.delete(db, edge_id)
    return Response(status_code=204)


# --------------------------------------------------------------------------- #
# Executions
# --------------------------------------------------------------------------- #
@router.post("/{workflow_id}/executions", summary="Create and start an execution")
async def create_execution(
    workflow_id: int, request: RunRequest | None = None, db: Session = Depends(get_db)
) -> Dict[str, Any]:
    _require_workflow(db, workflow_id)
    request = request or RunRequest()

    nodes = node_repo.get_by_workflow(db, workflow_id)
    edges = edge_repo.get_by_workflow(db, workflow_id)
    result = validate_graph(
        [n.id for n in nodes],
        [(e.source_id, e.target_id) for e in edges],
        max_nodes=settings.WORKFLOW_MAX_NODES,
    )
    result.raise_if_invalid()

    execution = workflow_execution_repo.create(
        db, WorkflowExecutionCreate(workflow_id=workflow_id, trigger=request.trigger)
    )

    if request.wait:
        summary = await workflow_engine.run_execution(execution.id)
        return {"execution_id": execution.id, **summary}

    workflow_engine.submit(execution.id)
    return {
        "execution_id": execution.id,
        "workflow_id": workflow_id,
        "status": ExecutionStatus.PENDING.value,
        "message": "Execution submitted.",
    }


@router.get("/{workflow_id}/executions", summary="List executions for a workflow")
def list_executions(
    workflow_id: int,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    _require_workflow(db, workflow_id)
    return workflow_execution_repo.get_by_workflow(db, workflow_id, skip=skip, limit=limit)


@router.get("/executions/{execution_id}", summary="Get execution status and node results")
def get_execution(execution_id: int, db: Session = Depends(get_db)) -> Dict[str, Any]:
    execution = workflow_execution_repo.get(db, execution_id)
    if not execution:
        raise NotFoundError(f"Execution {execution_id} not found.")
    node_executions = node_execution_repo.get_by_execution(db, execution_id)
    return {
        "id": execution.id,
        "workflow_id": execution.workflow_id,
        "status": execution.status.value if execution.status else None,
        "error": execution.error,
        "state": execution.state,
        "trigger": execution.trigger,
        "started_at": execution.started_at,
        "finished_at": execution.finished_at,
        "is_running": workflow_engine.is_running(execution_id),
        "node_executions": [
            {
                "node_id": ne.node_id,
                "status": ne.status.value if ne.status else None,
                "output_data": ne.output_data,
                "error": ne.error,
                "retry_count": ne.retry_count,
                "duration_ms": ne.duration_ms,
            }
            for ne in node_executions
        ],
    }


@router.post("/executions/{execution_id}/cancel", summary="Cancel a running execution")
def cancel_execution(execution_id: int, db: Session = Depends(get_db)) -> Dict[str, Any]:
    execution = workflow_execution_repo.get(db, execution_id)
    if not execution:
        raise NotFoundError(f"Execution {execution_id} not found.")
    cancelled = workflow_engine.cancel(execution_id)
    return {
        "execution_id": execution_id,
        "cancelled": cancelled,
        "message": "Cancellation requested." if cancelled else "Execution is not running.",
    }


# --------------------------------------------------------------------------- #
# V1.0 compatibility endpoint (kept verbatim)
# --------------------------------------------------------------------------- #
@router.post(
    "/{execution_id}/run",
    summary="Run an existing execution (V1.0 compatibility)",
    deprecated=True,
)
async def run_workflow(execution_id: int) -> Dict[str, Any]:
    """Deprecated: prefer ``POST /api/workflows/{workflow_id}/executions``."""
    workflow_engine.submit(execution_id)
    return {"status": "Execution submitted", "execution_id": execution_id}
