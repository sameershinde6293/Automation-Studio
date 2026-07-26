import React from 'react';
import { Volume2 } from 'lucide-react';
import { BaseNode } from './BaseNode';

/**
 * Text to Speech node.
 *
 * Config fields mirror the backend schema for `tts`
 * (see /api/system/node-schemas), so a node configured here validates
 * server-side without a translation step.
 */
export const TTSNode = (props: any) => (
  <BaseNode
    {...props}
    icon={<Volume2 size={16} />}
    color="#ec4899"
    inputs={['in']}
    outputs={['out']}
    configFields={[
      { key: 'text', label: 'Text', type: 'text' },
      { key: 'voice', label: 'Voice', type: 'text' },
      { key: 'language', label: 'Language', type: 'text', default: 'en' },
    ]}
  />
);
