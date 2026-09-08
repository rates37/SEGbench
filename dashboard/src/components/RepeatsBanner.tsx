import { useDashboard } from '../lib/dashboardCtx';

/** Persistent banner reading the repeats field (plan.md section 11 cross-cutting requirement):
 * repeats == 1 must state plainly that figures are single-run point estimates. */
export function RepeatsBanner() {
  const { doc } = useDashboard();
  const repeats = doc.campaign.repeats;
  if (repeats > 1) {
    return (
      <div className="border-b border-[var(--color-border)] bg-[var(--color-accent-muted)] px-4 py-2 text-sm">
        <strong>repeats = {repeats}.</strong> Figures below carry bootstrap confidence intervals
        where shown.
      </div>
    );
  }
  return (
    <div className="border-b border-[var(--color-warn-border)] bg-[var(--color-warn-bg)] px-4 py-2 text-sm">
      <strong>repeats = 1.</strong> Every figure below is a single-run point estimate. Differences
      within noise should not be read as rankings.
    </div>
  );
}
