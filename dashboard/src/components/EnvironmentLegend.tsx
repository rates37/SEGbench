import { ENV_DEFS } from '../lib/environments';

/** Compact, always-visible legend for the E0/E1/E2 shorthand used across every view (heatmap,
 * bug × model matrix, run drilldown, filters). Environment codes are otherwise unexplained
 * anywhere in the UI, which was confusing for anyone not already familiar with plan.md section 5. */
export function EnvironmentLegend() {
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1 border-b border-[var(--color-border)] bg-[var(--color-surface)] px-4 py-1.5 text-xs text-[var(--color-text-muted)]">
      <span className="font-medium text-[var(--color-text)]">environments:</span>
      {Object.entries(ENV_DEFS).map(([code, desc]) => (
        <span key={code}>
          <span className="font-medium text-[var(--color-text)]">{code}</span> = {desc}
        </span>
      ))}
    </div>
  );
}

