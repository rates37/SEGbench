import { useDashboard } from '../lib/dashboardCtx';
import { SpreadStat } from '../components/SpreadStat';

/** Colour-blind-safe diverging scale: orange (below grand mean) through neutral to blue (above),
 * never a rainbow. Missing cells render as an explicit dash, never as zero. */
function cellStyle(value: number | null, grandMean: number, maxAbs: number): React.CSSProperties {
  if (value === null || maxAbs === 0) return { background: 'var(--color-bg)' };
  const t = Math.max(-1, Math.min(1, (value - grandMean) / maxAbs));
  const color = t >= 0 ? 'var(--color-positive)' : 'var(--color-negative)';
  const alpha = Math.abs(t) * 0.55;
  return { background: color, opacity: 0.15 + alpha };
}

/** Model × environment heatmap (plan.md section 11): composite by model and environment, with
 * the E1-E0 and E2-E1 deltas called out — "how much does source access buy you". */
export function HeatmapView() {
  const { doc, modelEnvironment, modelDeltas } = useDashboard();
  const models = [...new Set(modelEnvironment.map((m) => m.model))].sort();
  const environments = doc.campaign.environments;

  const cellByKey = new Map(modelEnvironment.map((m) => [`${m.model}\u0000${m.environment}`, m]));
  const deltaByModel = new Map(modelDeltas.map((d) => [d.model, d]));

  const means = modelEnvironment.filter((m) => m.n_runs > 0).map((m) => m.composite.mean);
  const grandMean = means.length ? means.reduce((a, b) => a + b, 0) / means.length : 0;
  const maxAbs = means.length ? Math.max(...means.map((v) => Math.abs(v - grandMean))) || 1 : 1;

  if (models.length === 0) {
    return <p className="p-4 text-[var(--color-text-muted)]">No graded runs match the current filters.</p>;
  }

  return (
    <div className="p-4">
      <h2 className="mb-1 text-lg font-semibold">Model × environment heatmap</h2>
      <p className="mb-3 text-xs text-[var(--color-text-muted)]">
        Colour diverges around the grand mean ({grandMean.toFixed(2)}) — blue above, orange below.
        A blank cell is missing data, not a zero score.
      </p>
      <div className="overflow-x-auto rounded border border-[var(--color-border)] bg-[var(--color-surface)]">
        <table className="text-sm">
          <thead className="border-b border-[var(--color-border)]">
            <tr>
              <th className="px-3 py-2 text-left">model</th>
              {environments.map((env) => (
                <th key={env} className="px-3 py-2 text-left">
                  {env}
                </th>
              ))}
              <th className="px-3 py-2 text-left">Δ E1−E0</th>
              <th className="px-3 py-2 text-left">Δ E2−E1</th>
            </tr>
          </thead>
          <tbody>
            {models.map((model) => {
              const delta = deltaByModel.get(model);
              return (
                <tr key={model} className="border-b border-[var(--color-border)] last:border-0">
                  <td className="px-3 py-2 font-medium">{model}</td>
                  {environments.map((env) => {
                    const cell = cellByKey.get(`${model}\u0000${env}`);
                    const value = cell && cell.n_runs > 0 ? cell.composite.mean : null;
                    return (
                      <td key={env} className="px-3 py-2" style={cellStyle(value, grandMean, maxAbs)}>
                        {cell && cell.n_runs > 0 ? (
                          <SpreadStat spread={cell.composite} />
                        ) : (
                          <span className="text-[var(--color-text-muted)]">missing</span>
                        )}
                      </td>
                    );
                  })}
                  <td className="px-3 py-2">
                    {delta?.e1_minus_e0 !== null && delta?.e1_minus_e0 !== undefined
                      ? delta.e1_minus_e0.toFixed(2)
                      : '—'}
                  </td>
                  <td className="px-3 py-2">
                    {delta?.e2_minus_e1 !== null && delta?.e2_minus_e1 !== undefined
                      ? delta.e2_minus_e1.toFixed(2)
                      : '—'}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
