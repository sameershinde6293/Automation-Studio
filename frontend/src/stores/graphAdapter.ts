/**
 * Translation between the editor's graph shape and the backend's.
 *
 * These two schemas were mutually incompatible before M4 (gap I2):
 *
 *   editor  { id: "uuid-string", type, position: {x,y}, data: {label, config} }
 *   backend { id: 1,             node_type,  position_x, position_y, name, config }
 *
 * The editor also uses UUID **strings** for ids while the backend expects
 * **integers**, so edges could never resolve. Every save therefore failed with
 * HTTP 422. This module is the single place that conversion happens.
 */

import type { WorkflowEdge, WorkflowNode } from '../types/workflow';

export interface BackendNodePayload {
  id: number;
  name: string;
  node_type: string;
  config: Record<string, any>;
  position_x: number;
  position_y: number;
  retry_policy?: Record<string, any> | null;
}

export interface BackendEdgePayload {
  source_id: number;
  target_id: number;
  label?: string | null;
}

export interface GraphPayload {
  nodes: BackendNodePayload[];
  edges: BackendEdgePayload[];
}

export interface SerializedGraph extends GraphPayload {
  /** editor node id (string) -> the ordinal int id sent to the backend. */
  ordinalMap: Record<string, number>;
}

/**
 * Convert an editor graph into the backend's `PUT /graph` payload.
 *
 * Editor UUIDs are replaced with stable 1-based ordinals; the backend remaps
 * those onto real primary keys and returns an `id_map` we merge back in.
 * Edges referencing unknown nodes are dropped rather than sent, because the
 * backend rejects the whole graph if any edge dangles.
 */
export function serializeGraph(
  nodes: WorkflowNode[],
  edges: WorkflowEdge[],
): SerializedGraph {
  const ordinalMap: Record<string, number> = {};
  nodes.forEach((node, index) => {
    ordinalMap[node.id] = index + 1;
  });

  const payloadNodes: BackendNodePayload[] = nodes.map((node) => ({
    id: ordinalMap[node.id],
    name: node.data?.label || node.type || `Node ${ordinalMap[node.id]}`,
    node_type: node.type,
    config: node.data?.config ?? {},
    position_x: node.position?.x ?? 0,
    position_y: node.position?.y ?? 0,
    retry_policy: (node.data as any)?.retryPolicy ?? null,
  }));

  const payloadEdges: BackendEdgePayload[] = [];
  for (const edge of edges) {
    const source = ordinalMap[edge.source];
    const target = ordinalMap[edge.target];
    if (source === undefined || target === undefined) continue;
    payloadEdges.push({
      source_id: source,
      target_id: target,
      // React Flow's sourceHandle carries the branch name ("true"/"false"),
      // which is exactly what the engine's branch gating reads.
      label: edge.sourceHandle ?? null,
    });
  }

  return { nodes: payloadNodes, edges: payloadEdges, ordinalMap };
}

/**
 * Compose the editor-id -> backend-id map.
 *
 * `serializeGraph` maps editor ids to ordinals; the backend's response maps
 * those ordinals to real database ids. This joins the two.
 */
export function resolveIdMap(
  ordinalMap: Record<string, number>,
  backendIdMap: Record<string, number> | undefined,
): Record<string, number> {
  const resolved: Record<string, number> = {};
  for (const [editorId, ordinal] of Object.entries(ordinalMap)) {
    const backendId = backendIdMap?.[String(ordinal)];
    resolved[editorId] = backendId ?? ordinal;
  }
  return resolved;
}

/** Convert a backend graph response into editor nodes and edges. */
export function deserializeGraph(payload: {
  nodes?: any[];
  edges?: any[];
}): { nodes: WorkflowNode[]; edges: WorkflowEdge[]; idMap: Record<string, number> } {
  const idMap: Record<string, number> = {};

  const nodes: WorkflowNode[] = (payload.nodes ?? []).map((node) => {
    const editorId = String(node.id);
    idMap[editorId] = node.id;
    return {
      id: editorId,
      type: node.node_type,
      position: { x: node.position_x ?? 0, y: node.position_y ?? 0 },
      data: {
        label: node.name,
        config: node.config ?? {},
        ...(node.retry_policy ? { retryPolicy: node.retry_policy } : {}),
      },
    };
  });

  const edges: WorkflowEdge[] = (payload.edges ?? []).map((edge) => ({
    id: String(edge.id ?? `${edge.source_id}-${edge.target_id}`),
    source: String(edge.source_id),
    target: String(edge.target_id),
    ...(edge.label ? { sourceHandle: edge.label } : {}),
  }));

  return { nodes, edges, idMap };
}
