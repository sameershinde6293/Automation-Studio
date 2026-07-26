export interface Position {
  x: number;
  y: number;
}

export interface NodeData {
  label: string;
  config: Record<string, any>;
  description?: string;
  version?: string;
}

export interface WorkflowNode {
  id: string;
  type: string;
  position: Position;
  data: NodeData;
}

export interface WorkflowEdge {
  id: string;
  source: string;
  target: string;
  sourceHandle?: string;
  targetHandle?: string;
}

export interface Workflow {
  id: string;
  name: string;
  description?: string;
  nodes: WorkflowNode[];
  edges: WorkflowEdge[];
  version: number;
  createdAt: string;
  updatedAt: string;
  metadata?: Record<string, any>;
}

export type ExecutionStatus = 'idle' | 'running' | 'completed' | 'failed' | 'skipped';

export interface ExecutionState {
  nodeId: string;
  status: ExecutionStatus;
  startTime?: string;
  endTime?: string;
  duration?: number;
  error?: string;
  progress?: number;
}