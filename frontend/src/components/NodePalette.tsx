import React from 'react';

const nodeTypes = [
  'start', 'end', 'aiChat', 'aiCompletion', 'prompt', 'variable',
  'condition', 'loop', 'delay', 'httpRequest', 'webhook', 'python',
  'javascript', 'database', 'email', 'file', 'folder', 'imageGeneration',
  'tts', 'stt', 'ffmpeg', 'mediaProcessing'
];

export const NodePalette: React.FC = () => {
  const onDragStart = (event: React.DragEvent, nodeType: string) => {
    event.dataTransfer.setData('application/reactflow', nodeType);
    event.dataTransfer.effectAllowed = 'move';
  };

  return (
    <div style={{ width: 220, background: '#f8fafc', borderRight: '1px solid #e2e8f0', padding: 12, overflowY: 'auto' }}>
      <h3 style={{ margin: '0 0 12px' }}>Nodes</h3>
      {nodeTypes.map(type => (
        <div
          key={type}
          draggable
          onDragStart={(e) => onDragStart(e, type)}
          style={{
            padding: '8px 12px',
            marginBottom: 6,
            background: '#fff',
            border: '1px solid #cbd5e1',
            borderRadius: 6,
            cursor: 'grab',
            fontSize: 13
          }}
        >
          {type}
        </div>
      ))}
    </div>
  );
};