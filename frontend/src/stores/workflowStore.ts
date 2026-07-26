import { create } from 'zustand';
import { Workflow, WorkflowNode, WorkflowEdge, ExecutionState } from '../types/workflow';
import { v4 as uuidv4 } from 'uuid';

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
  resetExecution: () => void;
  setDirty: (dirty: boolean) => void;
}

export const useWorkflowStore = create<WorkflowStore>((set, get) => ({
  currentWorkflow: null,
  nodes: [],
  edges: [],
  executionStates: {},
  history: [],
  historyIndex: -1,
  isDirty: false,
  recentWorkflows: [],

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
    const payload = {
      name: currentWorkflow?.name || 'Untitled Workflow',
      nodes: nodes.map(n => ({ id: n.id, type: n.type, position: n.position, data: n.data })),
      edges: edges.map(e => ({ id: e.id, source: e.source, target: e.target })),
    };

    try {
      let res;
      if (currentWorkflow?.id) {
        res = await fetch(`${API_BASE}/${currentWorkflow.id}/graph`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
      } else {
        res = await fetch(`${API_BASE}/`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
      }
      const data = await res.json();
      const saved: Workflow = {
        ...payload,
        id: data.workflow_id || currentWorkflow?.id || uuidv4(),
        version: (currentWorkflow?.version || 0) + 1,
        createdAt: currentWorkflow?.createdAt || new Date().toISOString(),
        updatedAt: new Date().toISOString(),
      };
      set({ currentWorkflow: saved, isDirty: false });
      localStorage.setItem('currentWorkflow', JSON.stringify(saved));
    } catch (e) {
      console.warn('Backend save failed, falling back to localStorage');
      const saved = { ...get().currentWorkflow, nodes, edges, updatedAt: new Date().toISOString() };
      set({ currentWorkflow: saved, isDirty: false });
      localStorage.setItem('currentWorkflow', JSON.stringify(saved));
    }
  },

  loadWorkflow: (workflow) => {
    set({ currentWorkflow: workflow, nodes: workflow.nodes, edges: workflow.edges, isDirty: false });
  },

  loadFromBackend: async (id) => {
    try {
      const res = await fetch(`${API_BASE}/${id}/graph`);
      const data = await res.json();
      const wf: Workflow = {
        id: data.workflow.id,
        name: data.workflow.name,
        nodes: data.nodes.map((n: any) => ({ id: n.id, type: n.node_type, position: { x: n.position_x, y: n.position_y }, data: { label: n.name, config: n.config } })),
        edges: data.edges.map((e: any) => ({ id: e.id, source: e.source_id, target: e.target_id })),
        version: data.workflow.version,
        createdAt: new Date().toISOString(),
        updatedAt: new Date().toISOString(),
      };
      get().loadWorkflow(wf);
    } catch (e) {
      console.error('Failed to load from backend');
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
    const newHistory = history.slice(0, historyIndex + 1);
    newHistory.push({ nodes: [...nodes], edges: [...edges] });
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