import React, { useState } from 'react';
import './App.css';
import { EnterpriseFeatures } from './components/Enterprise';
import { AIAutomation } from './components/Automation';

function App() {
  const [activeTab, setActiveTab] = useState('workflows');

  return (
    <div className="App">
      <header className="App-header">
        <h1>Creator OS</h1>
        <nav>
          <button onClick={() => setActiveTab('workflows')} className={activeTab === 'workflows' ? 'active' : ''}>Workflows</button>
          <button onClick={() => setActiveTab('ai')} className={activeTab === 'ai' ? 'active' : ''}>AI</button>
          <button onClick={() => setActiveTab('media')} className={activeTab === 'media' ? 'active' : ''}>Media</button>
          <button onClick={() => setActiveTab('enterprise')} className={activeTab === 'enterprise' ? 'active' : ''}>Enterprise</button>
          <button onClick={() => setActiveTab('automation')} className={activeTab === 'automation' ? 'active' : ''}>Automation</button>
        </nav>
      </header>
      <main>
        {activeTab === 'workflows' && <div><h2>Workflow Engine</h2><p>DAG Execution & Orchestration.</p></div>}
        {activeTab === 'ai' && <div><h2>AI Runtime</h2><p>Model Registry & Prompt Orchestration.</p></div>}
        {activeTab === 'media' && <div><h2>Media Pipeline</h2><p>Asset Processing & Transcoding.</p></div>}
        {activeTab === 'enterprise' && <EnterpriseFeatures />}
        {activeTab === 'automation' && <AIAutomation />}
      </main>
    </div>
  );
}

export default App;
