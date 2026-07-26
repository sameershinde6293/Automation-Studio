import React from 'react';
import { Handle, Position } from 'reactflow';
import { Square } from 'lucide-react';

export const EndNode = ({ data }: any) => (
  <div className="node end-node" style={{ background: '#ef4444', padding: '10px', borderRadius: '8px', color: 'white' }}>
    <Handle type="target" position={Position.Left} />
    <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
      <Square size={16} /> <strong>{data.label || 'End'}</strong>
    </div>
  </div>
);