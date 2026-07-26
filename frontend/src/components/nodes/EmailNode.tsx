import React from 'react';
import { Mail } from 'lucide-react';
import { BaseNode } from './BaseNode';

/**
 * Email node.
 *
 * Config fields mirror the backend schema for `email`
 * (see /api/system/node-schemas), so a node configured here validates
 * server-side without a translation step.
 */
export const EmailNode = (props: any) => (
  <BaseNode
    {...props}
    icon={<Mail size={16} />}
    color="#10b981"
    inputs={['in']}
    outputs={['out']}
    configFields={[
      { key: 'to', label: 'To', type: 'text' },
      { key: 'subject', label: 'Subject', type: 'text' },
      { key: 'body', label: 'Body', type: 'text' },
    ]}
  />
);
