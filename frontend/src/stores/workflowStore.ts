import { create } from 'zustand';
import { Workflow, WorkflowNode, WorkflowEdge, ExecutionState } from '../types/workflow';
import { v4 as uuidv4 } from 'uuid';

interface WorkflowStore {
  currentWorkflow: Workflow | null;
  nodes: WorkflowNode[];
  edges: WorkflowEdge[];
  executionStates: Record<string, ExecutionState>;
  history: any[];
  historyIndex: number;

  setNodes: (nodes: WorkflowNode[]) => void;
  setEdges: (edges: WorkflowEdge[]) => void;
  addNode: (node: WorkflowNode) => void;
  deleteNode: (id: string) => void;
  addEdge: (edge: WorkflowEdge) => void;
  deleteEdge: (id: string) => void;
  saveWorkflow: () => void;
  loadWorkflow: (workflow: Workflow) => void;
  exportJSON: () => void;
  importJSON: (json: string) => void;
  undo: () => void;
  redo: () => void;
  updateExecutionState: (nodeId: string, state: Partial<ExecutionState>) => void;
  resetExecution: () => void;
}

export const useWorkflowStore = create<WorkflowStore>((set, get) => ({
  currentWorkflow: null,
  nodes: [],
  edges: [],
  executionStates: {},
  history: [],
  historyIndex: -1,

  setNodes: (nodes) => set({ nodes }),
  setEdges: (edges) => set({ edges }),

  addNode: (node) => {
    const nodes = [...get().nodes, node];
    set({ nodes });
    get().saveToHistory();
  },

  deleteNode: (id) => {
    const nodes = get().nodes.filter((n) => n.id !== id);
    const edges = get().edges.filter((e) => e.source !== id && e.target !== id);
    set({ nodes, edges });
    get().saveToHistory();
  },

  addEdge: (edge) => {
    const edges = [...get().edges, edge];
    set({ edges });
    get().saveToHistory();
  },

  deleteEdge: (id) => {
    const edges = get().edges.filter((e) => e.id !== id);
    set({ edges });
    get().saveToHistory();
  },

  saveWorkflow: () => {
    const { nodes, edges } = get();
    const workflow: Workflow = {
      id: get().currentWorkflow?.id || uuidv4(),
      name: get().currentWorkflow?.name || 'Untitled Workflow',
      nodes,
      edges,
      version: (get().currentWorkflow?.version || 0) + 1,
      createdAt: get().currentWorkflow?.createdAt || new Date().toISOString(),
      updatedAt: new Date().toISOString(),
    };
    set({ currentWorkflow: workflow });
    // Persist to localStorage for autosave demo
    localStorage.setItem('currentWorkflow', JSON.stringify(workflow));
    console.log('Workflow saved (autosave)');
  },

  loadWorkflow: (workflow) => {
    set({
      currentWorkflow: workflow,
      nodes: workflow.nodes,
      edges: workflow.edges,
    });
  },

  exportJSON: () => {
    const { currentWorkflow } = get();
    if (!currentWorkflow) return;
    const dataStr = JSON.stringify(currentWorkflow, null, 2);
    const dataUri = 'data:application/json;charset=utf-8,' + encodeURIComponent(dataStr);
    const link = document.createElement('a');
    link.href = dataUri;
    link.download = `${currentWorkflow.name}.json`;
    link.click();
  },

  importJSON: (json) => {
    try {
      const workflow = JSON.parse(json) as Workflow;
      get().loadWorkflow(workflow);
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
      set({
        nodes: prev.nodes,
        edges: prev.edges,
        historyIndex: historyIndex - 1,
      });
    }
  },

  redo: () => {
    const { historyIndex, history } = get();
    if (historyIndex < history.length - 1) {
      const next = history[historyIndex + 1];
      set({
        nodes: next.nodes,
        edges: next.edges,
        historyIndex: historyIndex + 1,
      });
    }
  },

  updateExecutionState: (nodeId, state) => {
    const executionStates = { ...get().executionStates };
    executionStates[nodeId] = { ...(executionStates[nodeId] || { nodeId, status: 'idle' }), ...state };
    set({ executionStates });
  },

  resetExecution: () => set({ executionStates: {} }),
}));