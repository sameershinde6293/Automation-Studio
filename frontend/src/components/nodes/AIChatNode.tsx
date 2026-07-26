import React from 'react';
import { Handle, Position } from 'reactflow';
import { MessageCircle } from 'lucide-react';

export const AIChatNode = ({ data }: any) => (
  <div className="node ai-chat-node" style={{ background: '#3b82f6', padding: '12px', borderRadius: '8px', color: 'white', minWidth: '140px' }}>
    <Handle type="target" position={Position.Left} />
    <Handle type="source" position={Position.Right} />
    <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
      <MessageCircle size={16} /> <strong>{data.label || 'AI Chat'}</strong>
    </div>
    <div style={{ fontSize: '10px', marginTop: '4px', opacity: 0.9 }}>{data.config?.model || 'gpt-4o'}</div>
  </div>
);