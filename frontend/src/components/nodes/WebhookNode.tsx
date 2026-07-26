import React from 'react';
import { Webhook } from 'lucide-react';
import { BaseNode } from './BaseNode';

/**
 * Webhook node.
 *
 * Config fields mirror the backend schema for `webhook`
 * (see /api/system/node-schemas), so a node configured here validates
 * server-side without a translation step.
 */
export const WebhookNode = (props: any) => (
  <BaseNode
    {...props}
    icon={<Webhook size={16} />}
    color="#06b6d4"
    inputs={['in']}
    outputs={['out']}
    configFields={[
      { key: 'url', label: 'URL', type: 'text' },
      { key: 'method', label: 'Method', type: 'text', default: 'POST' },
      { key: 'secret', label: 'Secret', type: 'text' },
    ]}
  />
);
