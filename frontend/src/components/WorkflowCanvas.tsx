import React, { useCallback, useRef } from 'react';
import ReactFlow, {
  Background,
  Controls,
  MiniMap,
  useNodesState,
  useEdgesState,
  addEdge,
  Connection,
  Edge,
  Node,
  ReactFlowProvider,
  Panel,
} from 'reactflow';
import 'reactflow/dist/style.css';
import { useWorkflowStore } from '../stores/workflowStore';
import { nodeTypes } from './nodes';
import { v4 as uuidv4 } from 'uuid';

const WorkflowCanvas: React.FC = () => {
  const { nodes, edges, setNodes, setEdges, addNode, deleteNode, undo, redo } = useWorkflowStore();
  const [rfNodes, setRfNodes, onNodesChange] = useNodesState(nodes);
  const [rfEdges, setRfEdges, onEdgesChange] = useEdgesState(edges);

  const reactFlowWrapper = useRef<HTMLDivElement>(null);

  const onConnect = useCallback(
    (params: Connection | Edge) => {
      const newEdge = { ...params, id: uuidv4() };
      setRfEdges((eds) => addEdge(newEdge, eds));
      useWorkflowStore.getState().addEdge(newEdge);
    },
    [setRfEdges]
  );

  const onDragOver = useCallback((event: React.DragEvent) => {
    event.preventDefault();
    event.dataTransfer.dropEffect = 'move';
  }, []);

  const onDrop = useCallback(
    (event: React.DragEvent) => {
      event.preventDefault();

      const type = event.dataTransfer.getData('application/reactflow');
      if (!type || !reactFlowWrapper.current) return;

      const reactFlowBounds = reactFlowWrapper.current.getBoundingClientRect();
      const position = {
        x: event.clientX - reactFlowBounds.left,
        y: event.clientY - reactFlowBounds.top,
      };

      const newNode: Node = {
        id: uuidv4(),
        type,
        position,
        data: { label: `${type} Node`, config: {}, description: `${type} description` },
      };

      addNode(newNode);
      setRfNodes((nds) => [...nds, newNode]);
    },
    [addNode, setRfNodes]
  );

  const onNodeContextMenu = useCallback(
    (event: React.MouseEvent, node: Node) => {
      event.preventDefault();
      if (window.confirm(`Delete node ${node.id}?`)) {
        deleteNode(node.id);
        setRfNodes((nds) => nds.filter((n) => n.id !== node.id));
      }
    },
    [deleteNode, setRfNodes]
  );

  // Keyboard shortcuts (simplified)
  React.useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Delete' || e.key === 'Backspace') {
        // delete selected
      }
      if ((e.ctrlKey || e.metaKey) && e.key === 'z') {
        e.preventDefault();
        undo();
      }
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'y') {
        e.preventDefault();
        redo();
      }
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
        e.preventDefault();
        useWorkflowStore.getState().saveWorkflow();
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [undo, redo]);

  return (
    <div ref={reactFlowWrapper} style={{ width: '100%', height: '100vh' }}>
      <ReactFlow
        nodes={rfNodes}
        edges={rfEdges}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onConnect={onConnect}
        onDrop={onDrop}
        onDragOver={onDragOver}
        onNodeContextMenu={onNodeContextMenu}
        nodeTypes={nodeTypes}
        fitView
        snapToGrid
        snapGrid={[15, 15]}
      >
        <Background variant="dots" gap={15} size={1} />
        <Controls />
        <MiniMap />
        <Panel position="top-right">
          <button onClick={() => useWorkflowStore.getState().saveWorkflow()}>Save</button>
          <button onClick={() => useWorkflowStore.getState().exportJSON()}>Export</button>
        </Panel>
      </ReactFlow>
    </div>
  );
};

export const WorkflowEditor = () => (
  <ReactFlowProvider>
    <WorkflowCanvas />
  </ReactFlowProvider>
);