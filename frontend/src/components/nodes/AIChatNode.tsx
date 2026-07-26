import React from 'react';
import { MessageSquare } from 'lucide-react';
import { BaseNode } from './BaseNode';

/**
 * AI Chat node.
 *
 * Config fields mirror the backend schema for `aiChat`
 * (see /api/system/node-schemas), so a node configured here validates
 * server-side without a translation step.
 */
export const AIChatNode = (props: any) => (
  <BaseNode
    {...props}
    icon={<MessageSquare size={16} />}
    color="#8b5cf6"
    inputs={['in']}
    outputs={['out']}
    configFields={[
      { key: 'prompt', label: 'Prompt', type: 'text' },
      { key: 'system', label: 'System', type: 'text' },
      { key: 'model', label: 'Model', type: 'text' },
      { key: 'temperature', label: 'Temperature', type: 'number' },
    ]}
  />
);
