import React, { useCallback, useRef, useEffect } from 'react';
import ReactFlow, {
  Background, Controls, MiniMap, useNodesState, useEdgesState,
  addEdge, ReactFlowProvider, Panel,
  useReactFlow,
} from 'reactflow';
// Type-only: these exist in reactflow's type namespace, not its runtime
// exports, so importing them as values makes the bundler warn.
import type { Connection, Edge, Node } from 'reactflow';
import 'reactflow/dist/style.css';
import { useWorkflowStore } from '../stores/workflowStore';
import { nodeTypes } from './nodes';
import { v4 as uuidv4 } from 'uuid';

const WorkflowCanvasInner: React.FC = () => {
  const { nodes, edges, setNodes, setEdges, addNode, deleteNode, undo, redo, copySelected, paste, duplicateSelected } = useWorkflowStore();
  const [rfNodes, setRfNodes, onNodesChange] = useNodesState(nodes);
  const [rfEdges, setRfEdges, onEdgesChange] = useEdgesState(edges);
  const reactFlowWrapper = useRef<HTMLDivElement>(null);
  const { fitView, zoomIn, zoomOut, zoomTo } = useReactFlow();

  const onConnect = useCallback((params: Connection | Edge) => {
    const newEdge = { ...params, id: uuidv4() };
    setRfEdges((eds) => addEdge(newEdge, eds));
    useWorkflowStore.getState().addEdge(newEdge);
  }, [setRfEdges]);

  const onDrop = useCallback((event: React.DragEvent) => {
    event.preventDefault();
    const type = event.dataTransfer.getData('application/reactflow');
    if (!type || !reactFlowWrapper.current) return;

    const bounds = reactFlowWrapper.current.getBoundingClientRect();
    const position = {
      x: event.clientX - bounds.left,
      y: event.clientY - bounds.top,
    };

    const newNode: Node = {
      id: uuidv4(),
      type,
      position,
      data: { label: `${type} Node`, config: {}, description: `${type} description` },
    };
    addNode(newNode);
    setRfNodes((nds) => [...nds, newNode]);
  }, [addNode, setRfNodes]);

  // Full keyboard shortcuts
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      const ctrl = e.ctrlKey || e.metaKey;
      if (e.key === 'Delete' || e.key === 'Backspace') {
        const selected = rfNodes.filter(n => n.selected);
        selected.forEach(n => deleteNode(n.id));
      }
      if (ctrl && e.key.toLowerCase() === 'c') { e.preventDefault(); copySelected(); }
      if (ctrl && e.key.toLowerCase() === 'v') { e.preventDefault(); paste(); }
      if (ctrl && e.key.toLowerCase() === 'd') { e.preventDefault(); duplicateSelected(); }
      if (ctrl && e.key === 'z') { e.preventDefault(); undo(); }
      if (ctrl && e.key.toLowerCase() === 'y' || (ctrl && e.shiftKey && e.key.toLowerCase() === 'z')) { e.preventDefault(); redo(); }
      if (ctrl && e.key.toLowerCase() === 'a') { e.preventDefault(); /* select all handled by React Flow */ }
      if (ctrl && e.key === '+') { e.preventDefault(); zoomIn(); }
      if (ctrl && e.key === '-') { e.preventDefault(); zoomOut(); }
      if (ctrl && e.key.toLowerCase() === 's') { e.preventDefault(); useWorkflowStore.getState().saveWorkflow(); }
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [rfNodes, deleteNode, undo, redo, copySelected, paste, duplicateSelected, zoomIn, zoomOut]);

  // Responsive + fit view on mount
  useEffect(() => {
    const resize = () => fitView({ padding: 0.2 });
    window.addEventListener('resize', resize);
    setTimeout(() => fitView({ padding: 0.2 }), 100);
    return () => window.removeEventListener('resize', resize);
  }, [fitView]);

  return (
    <div ref={reactFlowWrapper} style={{ width: '100%', height: '100vh' }}>
      <ReactFlow
        nodes={rfNodes}
        edges={rfEdges}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onConnect={onConnect}
        onDrop={onDrop}
        onDragOver={(e) => { e.preventDefault(); e.dataTransfer.dropEffect = 'move'; }}
        nodeTypes={nodeTypes}
        fitView
        snapToGrid
        snapGrid={[15, 15]}
        selectionOnDrag
        panOnDrag={[1, 2]}
        zoomOnScroll
        zoomOnPinch
        zoomOnDoubleClick
        selectNodesOnDrag
      >
        <Background variant="dots" gap={15} size={1} />
        <Controls />
        <MiniMap />
        <Panel position="top-right" style={{ display: 'flex', gap: 6 }}>
          <button onClick={() => fitView({ padding: 0.3 })}>Fit View</button>
          <button onClick={() => useWorkflowStore.getState().saveWorkflow()}>Save</button>
          <button onClick={() => useWorkflowStore.getState().exportJSON()}>Export</button>
        </Panel>
      </ReactFlow>
    </div>
  );
};

export const WorkflowEditor = () => (
  <ReactFlowProvider>
    <WorkflowCanvasInner />
  </ReactFlowProvider>
);