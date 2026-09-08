import { useState } from 'react';
import { useCampaignData } from './lib/data';
import { DashboardProvider } from './lib/DashboardContext';
import { readTabFromUrl } from './lib/filters';
import { RepeatsBanner } from './components/RepeatsBanner';
import { FilterBar } from './components/FilterBar';
import { OverviewView } from './views/OverviewView';
import { HeatmapView } from './views/HeatmapView';
import { InformationGainView } from './views/InformationGainView';
import { BugModelMatrixView } from './views/BugModelMatrixView';
import { FailureModesView } from './views/FailureModesView';
import { CostFrontierView } from './views/CostFrontierView';
import { RunDrilldownView } from './views/RunDrilldownView';

const TABS = [
  { id: 'overview', label: 'Overview', Component: OverviewView },
  { id: 'heatmap', label: 'Model × environment', Component: HeatmapView },
  { id: 'gain', label: 'Information gain', Component: InformationGainView },
  { id: 'matrix', label: 'Bug × model', Component: BugModelMatrixView },
  { id: 'failures', label: 'Failure modes', Component: FailureModesView },
  { id: 'frontier', label: 'Cost / score', Component: CostFrontierView },
  { id: 'runs', label: 'Run drilldown', Component: RunDrilldownView },
] as const;

function App() {
  const state = useCampaignData();
  const [tab, setTab] = useState(() => readTabFromUrl(TABS[0].id));

  if (state.status === 'loading') {
    return <p className="p-4 text-[var(--color-text-muted)]">Loading data.json…</p>;
  }
  if (state.status === 'error') {
    return (
      <p className="p-4 text-[var(--color-negative)]">
        Failed to load data.json: {state.message}. Run <code>segbench export</code> first.
      </p>
    );
  }

  const doc = state.data;
  const active = TABS.find((t) => t.id === tab) ?? TABS[0];

  if (doc.runs.length === 0) {
    return (
      <div className="flex min-h-screen flex-col">
        <header className="border-b border-[var(--color-border)] bg-[var(--color-surface)] px-4 py-3">
          <h1 className="text-xl font-semibold">segbench dashboard</h1>
        </header>
        <p className="p-4 text-[var(--color-text-muted)]">
          No runs in data.json yet — this campaign has not produced graded results. Run{' '}
          <code>segbench run</code> then <code>segbench grade --pending</code> then{' '}
          <code>segbench export</code>.
        </p>
      </div>
    );
  }

  return (
    <DashboardProvider doc={doc} tab={active.id}>
      <div className="flex min-h-screen flex-col">
        <header className="border-b border-[var(--color-border)] bg-[var(--color-surface)] px-4 py-3">
          <h1 className="text-xl font-semibold">segbench dashboard</h1>
          <p className="text-xs text-[var(--color-text-muted)]">
            generated {doc.campaign.generated_at} · judge {doc.campaign.judge_model} · corpus{' '}
            {doc.campaign.corpus_revision ?? 'unknown'}
          </p>
        </header>
        <RepeatsBanner />
        <FilterBar />
        <nav className="flex flex-wrap gap-1 border-b border-[var(--color-border)] bg-[var(--color-surface)] px-4 py-2 text-sm">
          {TABS.map((t) => (
            <button
              key={t.id}
              type="button"
              onClick={() => setTab(t.id)}
              className={`rounded px-3 py-1.5 ${t.id === active.id ? 'bg-[var(--color-accent)] text-white' : 'text-[var(--color-text-muted)] hover:bg-[var(--color-accent-muted)]'}`}
            >
              {t.label}
            </button>
          ))}
        </nav>
        <main className="flex-1">
          <active.Component />
        </main>
      </div>
    </DashboardProvider>
  );
}

export default App;

