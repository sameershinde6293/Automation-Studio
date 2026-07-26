import React from 'react';
import { Handle, Position } from 'reactflow';
export const PromptNode = ({ data }: any) => (
  <div style={{background:'#64748b',padding:'10px',borderRadius:'8px',color:'white'}}>
    <Handle type="target" position={Position.Left} /><Handle type="source" position={Position.Right} />
    <strong>Prompt</strong>
  </div>
);