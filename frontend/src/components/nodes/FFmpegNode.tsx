import React from 'react';
import { Film } from 'lucide-react';
import { BaseNode } from './BaseNode';

/**
 * FFmpeg node.
 *
 * Config fields mirror the backend schema for `ffmpeg`
 * (see /api/system/node-schemas), so a node configured here validates
 * server-side without a translation step.
 */
export const FFmpegNode = (props: any) => (
  <BaseNode
    {...props}
    icon={<Film size={16} />}
    color="#ec4899"
    inputs={['in']}
    outputs={['out']}
    configFields={[
      { key: 'input_path', label: 'Input Path', type: 'text' },
      { key: 'output_path', label: 'Output Path', type: 'text' },
      { key: 'operation', label: 'Operation', type: 'text', default: 'transcode' },
    ]}
  />
);
