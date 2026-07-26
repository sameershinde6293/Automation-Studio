import React from 'react';
import { Handle, Position } from 'reactflow';
import { Play } from 'lucide-react';

export const StartNode = ({ data }: any) => (
  <div className="node start-node" style={{ background: '#22c55e', padding: '10px', borderRadius: '8px', color: 'white' }}>
    <Handle type="source" position={Position.Right} />
    <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
      <Play size={16} /> <strong>{data.label || 'Start'}</strong>
    </div>
  </div>
);