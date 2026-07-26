import React from 'react';
import { Clapperboard } from 'lucide-react';
import { BaseNode } from './BaseNode';

/**
 * Media Processing node.
 *
 * Config fields mirror the backend schema for `mediaProcessing`
 * (see /api/system/node-schemas), so a node configured here validates
 * server-side without a translation step.
 */
export const MediaProcessingNode = (props: any) => (
  <BaseNode
    {...props}
    icon={<Clapperboard size={16} />}
    color="#ec4899"
    inputs={['in']}
    outputs={['out']}
    configFields={[
      { key: 'asset_id', label: 'Asset ID', type: 'number' },
      { key: 'path', label: 'Path', type: 'text' },
      { key: 'operation', label: 'Operation', type: 'text', default: 'process' },
    ]}
  />
);
