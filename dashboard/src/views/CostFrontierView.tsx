import { CartesianGrid, ResponsiveContainer, Scatter, ScatterChart, Tooltip, XAxis, YAxis, ZAxis } from 'recharts';
import { useDashboard } from '../lib/dashboardCtx';
import type { ModelAggregate } from '../lib/types';

/** Mean cost against mean score against a Pareto front — the practical procurement question is
 * which model is worth running, not which is best (plan.md section 11). */
function paretoFront(models: ModelAggregate[]): Set<string> {
  // A model is on the front if no other model has both lower cost and higher score.
  const front = new Set<string>();
  for (const m of models) {
    const dominated = models.some(
      (o) => o.model !== m.model && o.mean_cost_usd <= m.mean_cost_usd && o.composite.mean >= m.composite.mean && (o.mean_cost_usd < m.mean_cost_usd || o.composite.mean > m.composite.mean),
    );
    if (!dominated) front.add(m.model);
  }
  return front;
}

export function CostFrontierView() {
  const { models } = useDashboard();
  const front = paretoFront(models);

  if (models.length === 0) {
    return <p className="p-4 text-[var(--color-text-muted)]">No graded runs match the current filters.</p>;
  }

  const data = models.map((m) => ({
    model: m.model,
    cost: m.mean_cost_usd,
    score: m.composite.mean,
    n: m.composite.n,
    onFront: front.has(m.model),
  }));

  return (
    <div className="p-4">
      <h2 className="mb-1 text-lg font-semibold">Cost / score frontier</h2>
      <p className="mb-3 text-xs text-[var(--color-text-muted)]">
        Filled points sit on the Pareto front (no other model is both cheaper and better).
      </p>
      <ResponsiveContainer width="100%" height={360}>
        <ScatterChart margin={{ left: 16, right: 16, top: 16, bottom: 16 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="var(--color-border)" />
          <XAxis type="number" dataKey="cost" name="mean cost" unit="$" />
          <YAxis type="number" dataKey="score" name="mean composite score" domain={[0, 1]} />
          <ZAxis type="number" dataKey="n" range={[80, 300]} name="n" />
          <Tooltip
            cursor={{ strokeDasharray: '3 3' }}
            content={({ payload }) => {
              if (!payload || !payload.length) return null;
              const p = payload[0].payload as (typeof data)[number];
              return (
                <div className="rounded border border-[var(--color-border)] bg-[var(--color-surface)] px-2 py-1 text-xs shadow">
                  <div className="font-medium">{p.model}</div>
                  <div>cost: ${p.cost.toFixed(3)}</div>
                  <div>score: {p.score.toFixed(2)} (n={p.n})</div>
                  {p.onFront && <div className="text-[var(--color-positive)]">on Pareto front</div>}
                </div>
              );
            }}
          />
          <Scatter
            data={data}
            fill="var(--color-accent)"
            shape={(props: unknown) => {
              const p = props as { cx: number; cy: number; payload: (typeof data)[number] };
              return (
                <circle
                  cx={p.cx}
                  cy={p.cy}
                  r={7}
                  fill={p.payload.onFront ? 'var(--color-accent)' : 'var(--color-surface)'}
                  stroke="var(--color-accent)"
                  strokeWidth={2}
                />
              );
            }}
          />
        </ScatterChart>
      </ResponsiveContainer>
    </div>
  );
}
