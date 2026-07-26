import React from 'react';
import { File } from 'lucide-react';
import { BaseNode } from './BaseNode';

/**
 * File node.
 *
 * Config fields mirror the backend schema for `file`
 * (see /api/system/node-schemas), so a node configured here validates
 * server-side without a translation step.
 */
export const FileNode = (props: any) => (
  <BaseNode
    {...props}
    icon={<File size={16} />}
    color="#14b8a6"
    inputs={['in']}
    outputs={['out']}
    configFields={[
      { key: 'path', label: 'Path', type: 'text' },
      { key: 'operation', label: 'Operation', type: 'text', default: 'read' },
      { key: 'encoding', label: 'Encoding', type: 'text', default: 'utf-8' },
    ]}
  />
);
