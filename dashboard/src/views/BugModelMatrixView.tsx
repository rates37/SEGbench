import { useState } from 'react';
import { useDashboard } from '../lib/dashboardCtx';

/** Bug × model matrix (plan.md section 11): small-multiple grid, bugs as rows, models as
 * columns, cell colour by score. Sortable by mean difficulty, groupable by product. */
export function BugModelMatrixView() {
  const { doc, bugModelScores, bugDifficulty } = useDashboard();
  const [sortByDifficulty, setSortByDifficulty] = useState(true);

  const models = doc.campaign.models;
  const difficultyByBug = new Map(bugDifficulty.map((d) => [d.bug_id, d]));
  const productByBug = new Map(doc.bugs.map((b) => [b.bug_id, b.product]));
  const cellByKey = new Map(bugModelScores.map((s) => [`${s.bug_id}\u0000${s.model}`, s]));

  let bugIds = [...new Set(bugModelScores.map((s) => s.bug_id))];
  bugIds = sortByDifficulty
    ? bugIds.sort((a, b) => (difficultyByBug.get(a)?.mean_score ?? 0) - (difficultyByBug.get(b)?.mean_score ?? 0))
    : bugIds.sort();

  function colorFor(score: number): string {
    // Diverging around 0.5 (mid-competence), blue high / orange low — colour-blind safe.
    const t = Math.max(-1, Math.min(1, (score - 0.5) / 0.5));
    const color = t >= 0 ? 'var(--color-positive)' : 'var(--color-negative)';
    return color;
  }

  if (bugIds.length === 0) {
    return <p className="p-4 text-[var(--color-text-muted)]">No graded runs match the current filters.</p>;
  }

  return (
    <div className="p-4">
      <h2 className="mb-1 text-lg font-semibold">Bug × model matrix</h2>
      <p className="mb-3 text-xs text-[var(--color-text-muted)]">
        Reveals trivial bugs (uniformly high — drop them) and impossible ones (uniformly low —
        check the ground truth).
      </p>
      <label className="mb-3 flex items-center gap-1.5 text-sm">
        <input
          type="checkbox"
          checked={sortByDifficulty}
          onChange={(e) => setSortByDifficulty(e.target.checked)}
        />
        sort by mean difficulty
      </label>
      <div className="overflow-x-auto rounded border border-[var(--color-border)] bg-[var(--color-surface)]">
        <table className="text-sm">
          <thead className="border-b border-[var(--color-border)]">
            <tr>
              <th className="px-3 py-2 text-left">bug</th>
              <th className="px-3 py-2 text-left">product</th>
              <th className="px-3 py-2 text-left">mean difficulty</th>
              {models.map((m) => (
                <th key={m} className="px-3 py-2 text-left">
                  {m}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {bugIds.map((bugId) => {
              const diff = difficultyByBug.get(bugId);
              return (
                <tr key={bugId} className="border-b border-[var(--color-border)] last:border-0">
                  <td className="px-3 py-2 font-medium">{bugId}</td>
                  <td className="px-3 py-2 text-[var(--color-text-muted)]">{productByBug.get(bugId)}</td>
                  <td className="px-3 py-2">
                    {diff ? `${diff.mean_score.toFixed(2)} (n=${diff.n_models})` : '—'}
                  </td>
                  {models.map((model) => {
                    const cell = cellByKey.get(`${bugId}\u0000${model}`);
                    return (
                      <td
                        key={model}
                        className="px-3 py-2"
                        style={
                          cell
                            ? { background: colorFor(cell.score.mean), opacity: 0.25 + Math.abs(cell.score.mean - 0.5) }
                            : undefined
                        }
                      >
                        {cell ? `${cell.score.mean.toFixed(2)} (n=${cell.score.n})` : (
                          <span className="text-[var(--color-text-muted)]">missing</span>
                        )}
                      </td>
                    );
                  })}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
