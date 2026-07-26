import React from 'react';
import { FileText } from 'lucide-react';
import { BaseNode } from './BaseNode';

/**
 * Prompt node.
 *
 * Config fields mirror the backend schema for `prompt`
 * (see /api/system/node-schemas), so a node configured here validates
 * server-side without a translation step.
 */
export const PromptNode = (props: any) => (
  <BaseNode
    {...props}
    icon={<FileText size={16} />}
    color="#8b5cf6"
    inputs={['in']}
    outputs={['out']}
    configFields={[
      { key: 'template', label: 'Template', type: 'text' },
      { key: 'system', label: 'System', type: 'text' },
    ]}
  />
);
