import { Bar, BarChart, CartesianGrid, Legend, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import { useDashboard } from '../lib/dashboardCtx';
import { HelpButton } from '../components/HelpButton';

const OUTCOME_COLORS: Record<string, string> = {
  ok: 'var(--color-positive)',
  timeout: 'var(--color-warn-border)',
  no_answer: 'var(--color-negative)',
  invalid_answer: '#8b6fd6',
  cost_exceeded: '#b98b3f',
  harness_error: '#9a9aa2',
};

function colorFor(outcome: string): string {
  return OUTCOME_COLORS[outcome] ?? '#9a9aa2';
}

/** Failure-mode breakdown (plan.md section 11): stacked bars per model over outcome categories,
 * plus the hallucination axis (unsupported_specifics_rate) — how the model fails, not just how
 * often it succeeds. */
export function FailureModesView() {
  const { models } = useDashboard();

  const allOutcomes = [...new Set(models.flatMap((m) => Object.keys(m.outcomes.counts)))].sort();
  const rows = models.map((m) => {
    const row: Record<string, string | number> = { model: m.model };
    for (const o of allOutcomes) row[o] = m.outcomes.rates[o] ?? 0;
    return row;
  });

  if (models.length === 0) {
    return <p className="p-4 text-[var(--color-text-muted)]">No graded runs match the current filters.</p>;
  }

  return (
    <div className="p-4">
      <h2 className="mb-1 text-lg font-semibold">
        Failure-mode breakdown
        <HelpButton>
          Stacked bars: for each model, what fraction of its runs landed in each outcome (ok,
          timeout, no answer, invalid answer, cost exceeded). A model can score well on average but
          fail in a specific, informative way — e.g. never timing out but frequently producing no
          answer at all.
        </HelpButton>
      </h2>
      <p className="mb-3 text-xs text-[var(--color-text-muted)]">
        Outcome rate by model (stacked to 100%), plus the unsupported-specifics (hallucination)
        rate below.
      </p>
      <ResponsiveContainer width="100%" height={280}>
        <BarChart data={rows} layout="vertical" margin={{ left: 24 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="var(--color-border)" />
          <XAxis type="number" domain={[0, 1]} tickFormatter={(v) => `${(v * 100).toFixed(0)}%`} />
          <YAxis type="category" dataKey="model" width={160} />
          <Tooltip formatter={(v) => `${(Number(v) * 100).toFixed(1)}%`} />
          <Legend />
          {allOutcomes.map((o) => (
            <Bar key={o} dataKey={o} stackId="outcome" fill={colorFor(o)} />
          ))}
        </BarChart>
      </ResponsiveContainer>

      <table className="mt-4 w-full max-w-2xl text-sm">
        <thead>
          <tr className="border-b border-[var(--color-border)]">
            <th className="px-3 py-2 text-left">model</th>
            <th className="px-3 py-2 text-left">unsupported-specifics rate</th>
            <th className="px-3 py-2 text-left">leak-attempt rate</th>
          </tr>
        </thead>
        <tbody>
          {models.map((m) => (
            <tr key={m.model} className="border-b border-[var(--color-border)] last:border-0">
              <td className="px-3 py-2 font-medium">{m.model}</td>
              <td className="px-3 py-2">{(m.unsupported_specifics_rate * 100).toFixed(1)}%</td>
              <td className="px-3 py-2">{(m.leak_attempt_rate * 100).toFixed(1)}%</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
