import React from 'react';
import { Play } from 'lucide-react';
import { BaseNode } from './BaseNode';

export const StartNode = (props: any) => (
  <BaseNode
    {...props}
    icon={<Play size={16} />}
    color="#22c55e"
    outputs={['out']}
    configFields={[{ key: 'name', label: 'Workflow Name', type: 'text', default: 'Main' }]}
  />
);