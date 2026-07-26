import React from 'react';
import { Folder } from 'lucide-react';
import { BaseNode } from './BaseNode';

/**
 * Folder node.
 *
 * Config fields mirror the backend schema for `folder`
 * (see /api/system/node-schemas), so a node configured here validates
 * server-side without a translation step.
 */
export const FolderNode = (props: any) => (
  <BaseNode
    {...props}
    icon={<Folder size={16} />}
    color="#14b8a6"
    inputs={['in']}
    outputs={['out']}
    configFields={[
      { key: 'path', label: 'Path', type: 'text' },
      { key: 'operation', label: 'Operation', type: 'text', default: 'list' },
      { key: 'pattern', label: 'Pattern', type: 'text', default: '*' },
    ]}
  />
);
