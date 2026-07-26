import React from 'react';
import { Handle, Position } from 'reactflow';
export const AICompletionNode = ({ data }: any) => (
  <div style={{background:'#6366f1',padding:'10px',borderRadius:'8px',color:'white'}}>
    <Handle type="target" position={Position.Left} /><Handle type="source" position={Position.Right} />
    <strong>AI Completion</strong>
  </div>
);