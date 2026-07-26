import React from 'react';
import { Mic } from 'lucide-react';
import { BaseNode } from './BaseNode';

/**
 * Speech to Text node.
 *
 * Config fields mirror the backend schema for `stt`
 * (see /api/system/node-schemas), so a node configured here validates
 * server-side without a translation step.
 */
export const STTNode = (props: any) => (
  <BaseNode
    {...props}
    icon={<Mic size={16} />}
    color="#ec4899"
    inputs={['in']}
    outputs={['out']}
    configFields={[
      { key: 'audio_path', label: 'Audio Path', type: 'text' },
      { key: 'language', label: 'Language', type: 'text' },
    ]}
  />
);
