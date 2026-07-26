import React from 'react';
import { Handle, Position } from 'reactflow';
import type { NodeProps } from 'reactflow';
import { useWorkflowStore } from '../../stores/workflowStore';

interface BaseNodeProps extends NodeProps {
  icon: React.ReactNode;
  color: string;
  inputs?: string[];
  outputs?: string[];
  configFields?: Array<{ key: string; label: string; type: string; default?: any }>;
}

export const BaseNode: React.FC<BaseNodeProps> = ({ id, data, icon, color, inputs = [], outputs = [], configFields = [] }) => {
  const updateNode = useWorkflowStore((s) => s.updateNode);

  const handleConfigChange = (key: string, value: any) => {
    const newConfig = { ...(data.config || {}), [key]: value };
    updateNode(id, { data: { ...data, config: newConfig } });
  };

  return (
    <div
      style={{
        background: color,
        color: 'white',
        padding: '12px 16px',
        borderRadius: 8,
        minWidth: 160,
        border: '1px solid rgba(255,255,255,0.2)',
        boxShadow: '0 4px 6px rgba(0,0,0,0.1)',
      }}
    >
      {/* Inputs */}
      {inputs.map((h, i) => (
        <Handle key={i} type="target" position={Position.Left} id={h} style={{ top: 20 + i * 20 }} />
      ))}

      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
        {icon}
        <strong>{data.label}</strong>
      </div>

      {/* Config Fields */}
      {configFields.length > 0 && (
        <div style={{ fontSize: 11, marginTop: 8, background: 'rgba(255,255,255,0.15)', padding: 6, borderRadius: 4 }}>
          {configFields.map((field) => (
            <div key={field.key} style={{ marginBottom: 4 }}>
              <label style={{ display: 'block', fontSize: 10, opacity: 0.8 }}>{field.label}</label>
              <input
                type={field.type}
                value={data.config?.[field.key] ?? field.default ?? ''}
                onChange={(e) => handleConfigChange(field.key, e.target.value)}
                style={{ width: '100%', fontSize: 11, padding: 2, borderRadius: 3, border: 'none' }}
              />
            </div>
          ))}
        </div>
      )}

      {/* Outputs */}
      {outputs.map((h, i) => (
        <Handle key={i} type="source" position={Position.Right} id={h} style={{ top: 20 + i * 20 }} />
      ))}
    </div>
  );
};