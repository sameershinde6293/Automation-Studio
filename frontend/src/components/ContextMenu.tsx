import React from 'react';

interface ContextMenuProps {
  x: number;
  y: number;
  onClose: () => void;
  onAction: (action: string) => void;
}

export const ContextMenu: React.FC<ContextMenuProps> = ({ x, y, onClose, onAction }) => {
  const actions = ['Duplicate', 'Delete', 'Copy', 'Properties'];

  return (
    <div
      style={{
        position: 'absolute',
        left: x,
        top: y,
        background: '#1e2937',
        color: 'white',
        borderRadius: 6,
        boxShadow: '0 10px 15px rgba(0,0,0,0.3)',
        zIndex: 9999,
        minWidth: 160,
      }}
      onClick={onClose}
    >
      {actions.map((action) => (
        <div
          key={action}
          onClick={() => onAction(action.toLowerCase())}
          style={{ padding: '8px 16px', cursor: 'pointer' }}
          onMouseEnter={(e) => (e.currentTarget.style.background = '#334155')}
          onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}
        >
          {action}
        </div>
      ))}
    </div>
  );
};