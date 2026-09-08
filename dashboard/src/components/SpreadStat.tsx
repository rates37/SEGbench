import type { Spread } from '../lib/types';
import { stddev } from '../lib/format';

/** A mean is never shown bare: always with its spread and n (plan.md section 11 cross-cutting
 * requirement). Renders "—" for an empty aggregate rather than a misleading 0. */
export function SpreadStat({ spread, digits = 2 }: { spread: Spread; digits?: number }) {
  if (spread.n === 0) {
    return <span className="text-[var(--color-text-muted)]">— (n=0)</span>;
  }
  const sd = stddev(spread.values);
  return (
    <span title={`values: ${spread.values.map((v) => v.toFixed(2)).join(', ')}`}>
      {spread.mean.toFixed(digits)}
      <span className="text-[var(--color-text-muted)]"> ± {sd.toFixed(digits)}</span>
      {spread.ci95 && (
        <span className="text-[var(--color-text-muted)]">
          {' '}
          [{spread.ci95[0].toFixed(digits)}, {spread.ci95[1].toFixed(digits)}]
        </span>
      )}
      <span className="ml-1 rounded bg-[var(--color-accent-muted)] px-1 text-xs text-[var(--color-text-muted)]">
        n={spread.n}
      </span>
    </span>
  );
}
