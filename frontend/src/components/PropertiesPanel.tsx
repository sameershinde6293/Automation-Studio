import React from 'react';
import { useWorkflowStore } from '../stores/workflowStore';

export const PropertiesPanel: React.FC = () => {
  const { nodes, updateNode } = useWorkflowStore();
  const selected = nodes.find(n => (n as any).selected);

  if (!selected) return <div style={{ padding: 16, color: '#64748b' }}>Select a node to edit properties</div>;

  return (
    <div style={{ padding: 16, background: '#f8fafc', borderLeft: '1px solid #e2e8f0', width: 280 }}>
      <h4>Properties — {selected.data.label}</h4>
      <div style={{ marginTop: 12 }}>
        <label>Label</label>
        <input
          value={selected.data.label || ''}
          onChange={(e) => updateNode(selected.id, { data: { ...selected.data, label: e.target.value } })}
          style={{ width: '100%', padding: 6 }}
        />
      </div>
      {Object.entries(selected.data.config || {}).map(([k, v]) => (
        <div key={k} style={{ marginTop: 8 }}>
          <label>{k}</label>
          <input
            value={v as any}
            onChange={(e) => {
              const newConfig = { ...selected.data.config, [k]: e.target.value };
              updateNode(selected.id, { data: { ...selected.data, config: newConfig } });
            }}
            style={{ width: '100%', padding: 6 }}
          />
        </div>
      ))}
    </div>
  );
};