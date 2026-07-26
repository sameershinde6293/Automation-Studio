import React from 'react';
import { Image } from 'lucide-react';
import { BaseNode } from './BaseNode';

/**
 * Image Generation node.
 *
 * Config fields mirror the backend schema for `imageGeneration`
 * (see /api/system/node-schemas), so a node configured here validates
 * server-side without a translation step.
 */
export const ImageGenerationNode = (props: any) => (
  <BaseNode
    {...props}
    icon={<Image size={16} />}
    color="#8b5cf6"
    inputs={['in']}
    outputs={['out']}
    configFields={[
      { key: 'prompt', label: 'Prompt', type: 'text' },
      { key: 'width', label: 'Width', type: 'number', default: 1024 },
      { key: 'height', label: 'Height', type: 'number', default: 1024 },
    ]}
  />
);
