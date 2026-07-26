import React from 'react';
import { Square } from 'lucide-react';
import { BaseNode } from './BaseNode';

export const EndNode = (props: any) => (
  <BaseNode
    {...props}
    icon={<Square size={16} />}
    color="#ef4444"
    inputs={['in']}
    configFields={[{ key: 'label', label: 'Label', type: 'text', default: 'End' }]}
  />
);