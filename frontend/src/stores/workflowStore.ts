import { create } from 'zustand';
import type { Workflow, WorkflowNode, WorkflowEdge, ExecutionState } from '../types/workflow';
import { v4 as uuidv4 } from 'uuid';
import { deserializeGraph, resolveIdMap, serializeGraph } from './graphAdapter';

const API_BASE = 'http://localhost:8000/api/workflows';

interface WorkflowStore {
  currentWorkflow: Workflow | null;
  nodes: WorkflowNode[];
  edges: WorkflowEdge[];
  executionStates: Record<string, ExecutionState>;
  history: any[];
  historyIndex: number;
  isDirty: boolean;
  recentWorkflows: Workflow[];
  /** editor node id -> backend node id, produced by the last save/load. */
  nodeIdMap: Record<string, number>;
  lastSaveError: string | null;

  setNodes: (nodes: WorkflowNode[]) => void;
  setEdges: (edges: WorkflowEdge[]) => void;
  addNode: (node: WorkflowNode) => void;
  updateNode: (id: string, updates: Partial<WorkflowNode>) => void;
  deleteNode: (id: string) => void;
  addEdge: (edge: WorkflowEdge) => void;
  deleteEdge: (id: string) => void;
  saveWorkflow: () => Promise<void>;
  loadWorkflow: (workflow: Workflow) => void;
  loadFromBackend: (id: string) => Promise<void>;
  exportJSON: () => void;
  importJSON: (json: string) => void;
  undo: () => void;
  redo: () => void;
  copySelected: () => void;
  paste: () => void;
  duplicateSelected: () => void;
  updateExecutionState: (nodeId: string, state: Partial<ExecutionState>) => void;
  saveToHistory: () => void;
  resetExecution: () => void;
  setDirty: (dirty: boolean) => void;
}

export const useWorkflowStore = create<WorkflowStore>((set, get) => ({
  currentWorkflow: null,
  nodes: [],
  edges: [],
  executionStates: {},
  // M4 fix: the undo stack must start with a baseline snapshot. Previously it
  // began empty with historyIndex -1, so the *first* edit could never be
  // undone (undo requires historyIndex > 0) and redo could never restore it.
  // The M3 test for this existed but no runner was configured, so it never ran.
  history: [{ nodes: [], edges: [] }],
  historyIndex: 0,
  isDirty: false,
  recentWorkflows: [],
  nodeIdMap: {},
  lastSaveError: null,

  setNodes: (nodes) => { set({ nodes, isDirty: true }); get().saveToHistory(); },
  setEdges: (edges) => { set({ edges, isDirty: true }); get().saveToHistory(); },

  addNode: (node) => {
    const nodes = [...get().nodes, node];
    set({ nodes, isDirty: true });
    get().saveToHistory();
  },

  updateNode: (id, updates) => {
    const nodes = get().nodes.map(n => n.id === id ? { ...n, ...updates } : n);
    set({ nodes, isDirty: true });
  },

  deleteNode: (id) => {
    const nodes = get().nodes.filter((n) => n.id !== id);
    const edges = get().edges.filter((e) => e.source !== id && e.target !== id);
    set({ nodes, edges, isDirty: true });
    get().saveToHistory();
  },

  addEdge: (edge) => {
    const edges = [...get().edges, edge];
    set({ edges, isDirty: true });
    get().saveToHistory();
  },

  deleteEdge: (id) => {
    const edges = get().edges.filter((e) => e.id !== id);
    set({ edges, isDirty: true });
    get().saveToHistory();
  },

  saveWorkflow: async () => {
    const { nodes, edges, currentWorkflow } = get();

    // M4: the editor graph must be translated into the backend's schema
    // (node_type / position_x / source_id, integer ids). Sending the raw
    // editor shape returned HTTP 422 for every save before this.
    const { nodes: apiNodes, edges: apiEdges, ordinalMap } = serializeGraph(nodes, edges);

    try {
      let workflowId = currentWorkflow?.id;

      if (!workflowId) {
        const created = await fetch(`${API_BASE}/`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            name: currentWorkflow?.name || 'Untitled Workflow',
            description: currentWorkflow?.description,
          }),
        });
        if (!created.ok) throw new Error(`Create failed: ${created.status}`);
        workflowId = (await created.json()).id;
      }

      const res = await fetch(`${API_BASE}/${workflowId}/graph`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ nodes: apiNodes, edges: apiEdges }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body?.error?.message || `Save failed: ${res.status}`);
      }
      const data = await res.json();

      const saved: Workflow = {
        ...(currentWorkflow ?? ({} as Workflow)),
        id: workflowId as any,
        name: currentWorkflow?.name || 'Untitled Workflow',
        nodes,
        edges,
        version: (currentWorkflow?.version || 0) + 1,
        createdAt: currentWorkflow?.createdAt || new Date().toISOString(),
        updatedAt: new Date().toISOString(),
      };
      set({ currentWorkflow: saved, isDirty: false, lastSaveError: null });
      // Publish the editor-id -> backend-id map so live execution events can
      // be matched back to canvas nodes.
      set({ nodeIdMap: resolveIdMap(ordinalMap, data?.id_map) });
      localStorage.setItem('currentWorkflow', JSON.stringify(saved));
    } catch (e) {
      const message = (e as Error).message || 'Backend save failed';
      console.warn('Backend save failed, falling back to localStorage:', message);
      const saved = { ...get().currentWorkflow, nodes, edges, updatedAt: new Date().toISOString() } as Workflow;
      set({ currentWorkflow: saved, isDirty: false, lastSaveError: message });
      localStorage.setItem('currentWorkflow', JSON.stringify(saved));
    }
  },

  loadWorkflow: (workflow) => {
    set({ currentWorkflow: workflow, nodes: workflow.nodes, edges: workflow.edges, isDirty: false });
  },

  loadFromBackend: async (id) => {
    try {
      const res = await fetch(`${API_BASE}/${id}/graph`);
      if (!res.ok) throw new Error(`Load failed: ${res.status}`);
      const data = await res.json();
      const { nodes, edges, idMap } = deserializeGraph(data);
      const wf: Workflow = {
        id: data.workflow.id,
        name: data.workflow.name,
        description: data.workflow.description,
        nodes,
        edges,
        version: data.workflow.version,
        createdAt: new Date().toISOString(),
        updatedAt: new Date().toISOString(),
      };
      get().loadWorkflow(wf);
      set({ nodeIdMap: idMap });
    } catch (e) {
      console.error('Failed to load from backend:', (e as Error).message);
      set({ lastSaveError: (e as Error).message });
    }
  },

  exportJSON: () => {
    const { currentWorkflow } = get();
    if (!currentWorkflow) return;
    const dataStr = JSON.stringify(currentWorkflow, null, 2);
    const link = document.createElement('a');
    link.href = 'data:application/json;charset=utf-8,' + encodeURIComponent(dataStr);
    link.download = `${currentWorkflow.name}.json`;
    link.click();
  },

  importJSON: (json) => {
    try {
      const workflow = JSON.parse(json) as Workflow;
      get().loadWorkflow(workflow);
      set({ isDirty: true });
    } catch (e) {
      alert('Invalid workflow JSON');
    }
  },

  saveToHistory: () => {
    const { nodes, edges, history, historyIndex } = get();
    // M4 fix: guarantee a baseline entry exists before recording the new state.
    // Without it the first edit landed at index 0 and `undo` (which requires
    // historyIndex > 0) could never step back past it. Seeding here rather than
    // only at store creation keeps undo working after a direct setState, which
    // is how the editor restores a workflow and how tests reset state.
    const base =
      history.length === 0
        ? [{ nodes: [], edges: [] }]
        : history.slice(0, historyIndex + 1);
    const newHistory = [...base, { nodes: [...nodes], edges: [...edges] }];
    set({ history: newHistory, historyIndex: newHistory.length - 1 });
  },

  undo: () => {
    const { historyIndex, history } = get();
    if (historyIndex > 0) {
      const prev = history[historyIndex - 1];
      set({ nodes: prev.nodes, edges: prev.edges, historyIndex: historyIndex - 1, isDirty: true });
    }
  },

  redo: () => {
    const { historyIndex, history } = get();
    if (historyIndex < history.length - 1) {
      const next = history[historyIndex + 1];
      set({ nodes: next.nodes, edges: next.edges, historyIndex: historyIndex + 1, isDirty: true });
    }
  },

  copySelected: () => {
    const selected = get().nodes.filter(n => (n as any).selected);
    if (selected.length) localStorage.setItem('workflowClipboard', JSON.stringify(selected));
  },

  paste: () => {
    const clip = localStorage.getItem('workflowClipboard');
    if (!clip) return;
    const copied = JSON.parse(clip);
    const newNodes = copied.map((n: any) => ({ ...n, id: uuidv4(), position: { x: n.position.x + 40, y: n.position.y + 40 } }));
    set({ nodes: [...get().nodes, ...newNodes], isDirty: true });
  },

  duplicateSelected: () => {
    const selected = get().nodes.filter(n => (n as any).selected);
    const newNodes = selected.map(n => ({ ...n, id: uuidv4(), position: { x: n.position.x + 50, y: n.position.y + 50 } }));
    set({ nodes: [...get().nodes, ...newNodes], isDirty: true });
  },

  updateExecutionState: (nodeId, state) => {
    const executionStates = { ...get().executionStates };
    executionStates[nodeId] = { ...(executionStates[nodeId] || { nodeId, status: 'idle' }), ...state };
    set({ executionStates });
  },

  resetExecution: () => set({ executionStates: {} }),
  setDirty: (dirty) => set({ isDirty: dirty }),
}));