import React, { useCallback, useEffect, useRef, useState } from 'react';
import './App.css';
import { EnterpriseFeatures } from './components/Enterprise';
import { AIAutomation } from './components/Automation';
import { WorkflowEditor } from './components/WorkflowEditor';
import { ErrorBoundary } from './components/ErrorBoundary';
import { API_ROOT } from './api/executionApi';

type TabId = 'workflows' | 'ai' | 'media' | 'enterprise' | 'automation';

const TABS: { id: TabId; label: string }[] = [
  { id: 'workflows', label: 'Workflows' },
  { id: 'ai', label: 'AI' },
  { id: 'media', label: 'Media' },
  { id: 'enterprise', label: 'Enterprise' },
  { id: 'automation', label: 'Automation' },
];

type HealthState = 'checking' | 'online' | 'offline';

const HEALTH_URL = `${API_ROOT.replace(/\/api\/?$/, '')}/health`;

/**
 * Backend health indicator.
 *
 * M5 changes: the fetch is abortable and its result is discarded after
 * unmount (the M4 version called setState unconditionally, which warns and
 * leaks in StrictMode), it retries on demand, and it reports a machine
 * readable state rather than an arbitrary string.
 */
function useBackendHealth() {
  const [state, setState] = useState<HealthState>('checking');
  const [attempt, setAttempt] = useState(0);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 5000);

    setState('checking');
    fetch(HEALTH_URL, { signal: controller.signal })
      .then((response) => (response.ok ? response.json() : Promise.reject(response.status)))
      .then((data) => {
        if (mounted.current) setState(data?.status === 'healthy' ? 'online' : 'offline');
      })
      .catch(() => {
        if (mounted.current) setState('offline');
      })
      .finally(() => clearTimeout(timer));

    return () => {
      mounted.current = false;
      clearTimeout(timer);
      controller.abort();
    };
  }, [attempt]);

  const retry = useCallback(() => setAttempt((n) => n + 1), []);
  return { state, retry };
}

function App() {
  const [activeTab, setActiveTab] = useState<TabId>('workflows');
  const { state: health, retry } = useBackendHealth();
  const tabRefs = useRef<Record<string, HTMLButtonElement | null>>({});

  /**
   * Arrow-key navigation between tabs, per the WAI-ARIA tabs pattern.
   * Previously the tabs were plain buttons reachable only by repeated Tab.
   */
  const onTabKeyDown = (event: React.KeyboardEvent, index: number) => {
    const last = TABS.length - 1;
    let next: number | null = null;
    if (event.key === 'ArrowRight') next = index === last ? 0 : index + 1;
    if (event.key === 'ArrowLeft') next = index === 0 ? last : index - 1;
    if (event.key === 'Home') next = 0;
    if (event.key === 'End') next = last;
    if (next === null) return;
    event.preventDefault();
    const target = TABS[next];
    setActiveTab(target.id);
    tabRefs.current[target.id]?.focus();
  };

  const healthLabel =
    health === 'online' ? 'Online' : health === 'checking' ? 'Checking…' : 'Backend offline';

  return (
    <div className="App">
      <header className="App-header">
        <h1>Creator OS</h1>
        <div
          style={{ fontSize: '0.8em', marginBottom: 10, display: 'flex', gap: 8, alignItems: 'center' }}
        >
          <span
            data-testid="health-dot"
            aria-hidden="true"
            style={{
              width: 8,
              height: 8,
              borderRadius: '50%',
              background:
                health === 'online' ? '#22c55e' : health === 'checking' ? '#eab308' : '#ef4444',
            }}
          />
          <span data-testid="health-status" role="status" aria-live="polite">
            Backend Status: {healthLabel}
          </span>
          {health === 'offline' && (
            <button type="button" onClick={retry} data-testid="health-retry">
              Retry
            </button>
          )}
        </div>

        <nav role="tablist" aria-label="Main sections">
          {TABS.map((tab, index) => (
            <button
              key={tab.id}
              ref={(element) => {
                tabRefs.current[tab.id] = element;
              }}
              role="tab"
              id={`tab-${tab.id}`}
              aria-selected={activeTab === tab.id}
              aria-controls={`panel-${tab.id}`}
              tabIndex={activeTab === tab.id ? 0 : -1}
              className={activeTab === tab.id ? 'active' : ''}
              onClick={() => setActiveTab(tab.id)}
              onKeyDown={(event) => onTabKeyDown(event, index)}
            >
              {tab.label}
            </button>
          ))}
        </nav>
      </header>

      <main
        role="tabpanel"
        id={`panel-${activeTab}`}
        aria-labelledby={`tab-${activeTab}`}
        tabIndex={0}
      >
        {/* Each panel gets its own boundary so a failure in one section
            cannot take down the shell or the other tabs. */}
        {activeTab === 'workflows' && (
          <ErrorBoundary name="Workflow editor">
            <WorkflowEditor />
          </ErrorBoundary>
        )}
        {activeTab === 'ai' && (
          <div>
            <h2>AI Runtime</h2>
            <p>Model registry and prompt orchestration are active.</p>
          </div>
        )}
        {activeTab === 'media' && (
          <div>
            <h2>Media Pipeline</h2>
            <p>Asset processing and transcoding are active.</p>
          </div>
        )}
        {activeTab === 'enterprise' && (
          <ErrorBoundary name="Enterprise">
            <EnterpriseFeatures />
          </ErrorBoundary>
        )}
        {activeTab === 'automation' && (
          <ErrorBoundary name="Automation">
            <AIAutomation />
          </ErrorBoundary>
        )}
      </main>
    </div>
  );
}

export default App;
