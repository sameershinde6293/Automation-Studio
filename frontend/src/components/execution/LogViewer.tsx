import React, { useEffect, useMemo, useRef, useState } from 'react';
import { useExecutionStore } from '../../stores/executionStore';
import type { LogRecord } from '../../api/executionApi';

const LEVEL_COLOURS: Record<string, string> = {
  DEBUG: '#64748b',
  INFO: '#94a3b8',
  WARNING: '#f59e0b',
  ERROR: '#ef4444',
};

const LEVELS = ['ALL', 'DEBUG', 'INFO', 'WARNING', 'ERROR'] as const;

/**
 * Streaming log viewer with level filtering, text search and auto-scroll.
 *
 * Auto-scroll suspends as soon as the user scrolls away from the bottom, so
 * reading history is not fighting the incoming stream.
 */
export const LogViewer: React.FC = () => {
  const logs = useExecutionStore((s) => s.logs);
  const [level, setLevel] = useState<(typeof LEVELS)[number]>('ALL');
  const [query, setQuery] = useState('');
  const [autoScroll, setAutoScroll] = useState(true);
  const containerRef = useRef<HTMLDivElement>(null);

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return logs.filter((log: LogRecord) => {
      if (level !== 'ALL' && log.level !== level) return false;
      if (needle && !log.message.toLowerCase().includes(needle)) return false;
      return true;
    });
  }, [logs, level, query]);

  useEffect(() => {
    if (!autoScroll || !containerRef.current) return;
    containerRef.current.scrollTop = containerRef.current.scrollHeight;
  }, [filtered.length, autoScroll]);

  const onScroll = () => {
    const element = containerRef.current;
    if (!element) return;
    const atBottom =
      element.scrollHeight - element.scrollTop - element.clientHeight < 32;
    setAutoScroll(atBottom);
  };

  return (
    <div data-testid="log-viewer" style={{ display: 'flex', flexDirection: 'column', minHeight: 0 }}>
      <div style={{ display: 'flex', gap: 6, marginBottom: 6, alignItems: 'center' }}>
        <select
          aria-label="Log level filter"
          data-testid="log-level-filter"
          value={level}
          onChange={(e) => setLevel(e.target.value as (typeof LEVELS)[number])}
          style={{ fontSize: 11, background: '#1e293b', color: '#e2e8f0', border: '1px solid #334155', borderRadius: 4, padding: '3px 6px' }}
        >
          {LEVELS.map((option) => (
            <option key={option} value={option}>
              {option}
            </option>
          ))}
        </select>

        <input
          aria-label="Search logs"
          data-testid="log-search"
          placeholder="Search…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          style={{ flex: 1, fontSize: 11, background: '#1e293b', color: '#e2e8f0', border: '1px solid #334155', borderRadius: 4, padding: '3px 6px' }}
        />

        <label style={{ fontSize: 10, color: '#94a3b8', display: 'flex', gap: 4, alignItems: 'center' }}>
          <input
            type="checkbox"
            data-testid="log-autoscroll"
            checked={autoScroll}
            onChange={(e) => setAutoScroll(e.target.checked)}
          />
          Follow
        </label>
      </div>

      <div
        ref={containerRef}
        onScroll={onScroll}
        data-testid="log-list"
        style={{
          flex: 1,
          overflowY: 'auto',
          background: '#0b1220',
          border: '1px solid #1e293b',
          borderRadius: 6,
          padding: 8,
          fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
          fontSize: 11,
          minHeight: 140,
          maxHeight: 260,
        }}
      >
        {filtered.length === 0 ? (
          <div data-testid="log-empty" style={{ color: '#475569' }}>
            {logs.length === 0 ? 'No logs yet.' : 'No logs match the current filter.'}
          </div>
        ) : (
          filtered.map((log, index) => (
            <div
              key={`${log.sequence}-${index}`}
              data-testid="log-line"
              style={{ display: 'flex', gap: 8, marginBottom: 2, whiteSpace: 'pre-wrap' }}
            >
              <span style={{ color: LEVEL_COLOURS[log.level] ?? '#94a3b8', flexShrink: 0 }}>
                [{log.level}]
              </span>
              <span style={{ color: '#cbd5e1' }}>{log.message}</span>
            </div>
          ))
        )}
      </div>

      <div style={{ fontSize: 10, color: '#475569', marginTop: 4 }}>
        {filtered.length} of {logs.length} line(s)
      </div>
    </div>
  );
};
