import { useState } from 'react';
import { useDashboard } from '../lib/dashboardCtx';
import { SpreadStat } from '../components/SpreadStat';
import { formatPct, formatUsd, formatSeconds } from '../lib/format';
import type { ModelAggregate } from '../lib/types';

type SortKey = 'composite' | 'diagnosis' | 'localisation' | 'remedy' | 'mean_cost_usd' | 'mean_latency_s';

function sortValue(m: ModelAggregate, key: SortKey): number {
  if (key === 'mean_cost_usd' || key === 'mean_latency_s') return m[key];
  return m[key].mean;
}

/** Overview / leaderboard (plan.md section 11): one row per model, composite + three subscores,
 * mean cost, mean latency, timeout/no-answer/leak-attempt rates. Sortable. */
export function OverviewView() {
  const { models } = useDashboard();
  const [sortKey, setSortKey] = useState<SortKey>('composite');
  const [desc, setDesc] = useState(true);

  const sorted = [...models].sort((a, b) => {
    const diff = sortValue(a, sortKey) - sortValue(b, sortKey);
    return desc ? -diff : diff;
  });

  function headerClick(key: SortKey) {
    if (key === sortKey) setDesc((d) => !d);
    else {
      setSortKey(key);
      setDesc(true);
    }
  }

  const th = (key: SortKey, label: string) => (
    <th
      className="cursor-pointer select-none px-3 py-2 text-left hover:bg-[var(--color-accent-muted)]"
      onClick={() => headerClick(key)}
    >
      {label}
      {sortKey === key ? (desc ? ' ↓' : ' ↑') : ''}
    </th>
  );

  if (models.length === 0) {
    return <p className="p-4 text-[var(--color-text-muted)]">No graded runs match the current filters.</p>;
  }

  return (
    <div className="p-4">
      <h2 className="mb-3 text-lg font-semibold">Overview / leaderboard</h2>
      <div className="overflow-x-auto rounded border border-[var(--color-border)] bg-[var(--color-surface)]">
        <table className="w-full text-sm">
          <thead className="border-b border-[var(--color-border)] bg-[var(--color-bg)]">
            <tr>
              <th className="px-3 py-2 text-left">model</th>
              {th('composite', 'composite')}
              {th('diagnosis', 'diagnosis')}
              {th('localisation', 'localisation')}
              {th('remedy', 'remedy')}
              {th('mean_cost_usd', 'mean cost')}
              {th('mean_latency_s', 'mean latency')}
              <th className="px-3 py-2 text-left">timeout</th>
              <th className="px-3 py-2 text-left">no answer</th>
              <th className="px-3 py-2 text-left">leak attempts</th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((m) => (
              <tr key={m.model} className="border-b border-[var(--color-border)] last:border-0">
                <td className="px-3 py-2 font-medium">
                  {m.model}
                  <span className="ml-1 text-xs text-[var(--color-text-muted)]">(n_runs={m.n_runs})</span>
                </td>
                <td className="px-3 py-2">
                  <SpreadStat spread={m.composite} />
                </td>
                <td className="px-3 py-2">
                  <SpreadStat spread={m.diagnosis} />
                </td>
                <td className="px-3 py-2">
                  <SpreadStat spread={m.localisation} />
                </td>
                <td className="px-3 py-2">
                  <SpreadStat spread={m.remedy} />
                </td>
                <td className="px-3 py-2">{formatUsd(m.mean_cost_usd)}</td>
                <td className="px-3 py-2">{formatSeconds(m.mean_latency_s)}</td>
                <td className="px-3 py-2">{formatPct(m.outcomes.rates.timeout ?? 0)}</td>
                <td className="px-3 py-2">{formatPct(m.outcomes.rates.no_answer ?? 0)}</td>
                <td className="px-3 py-2">{formatPct(m.leak_attempt_rate)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
