import { useDashboard } from '../lib/dashboardCtx';
import { environmentTitle } from '../lib/environments';

function Select({
  label,
  value,
  options,
  onChange,
  optionTitle,
}: {
  label: string;
  value: string | null;
  options: string[];
  onChange: (v: string | null) => void;
  optionTitle?: (o: string) => string | undefined;
}) {
  return (
    <label className="flex items-center gap-1.5 text-sm">
      <span className="text-[var(--color-text-muted)]">{label}</span>
      <select
        className="rounded border border-[var(--color-border)] bg-[var(--color-surface)] px-2 py-1 text-sm"
        value={value ?? ''}
        onChange={(e) => onChange(e.target.value || null)}
      >
        <option value="">all</option>
        {options.map((o) => (
          <option key={o} value={o} title={optionTitle?.(o)}>
            {o}
          </option>
        ))}
      </select>
    </label>
  );
}

/** Global filters (model, environment, product, tag) applying across every view (plan.md
 * section 11 cross-cutting requirement), with state mirrored to the URL. */
export function FilterBar() {
  const { doc, filters, setFilter, clearFilters, filtered, runs } = useDashboard();

  const products = [...new Set(doc.bugs.map((b) => b.product))].sort();
  const tags = [...new Set(doc.bugs.flatMap((b) => b.tags))].sort();

  return (
    <div className="flex flex-wrap items-center gap-3 border-b border-[var(--color-border)] bg-[var(--color-surface)] px-4 py-2">
      <Select
        label="model"
        value={filters.model}
        options={doc.campaign.models}
        onChange={(v) => setFilter({ model: v })}
      />
      <Select
        label="environment"
        value={filters.environment}
        options={doc.campaign.environments}
        onChange={(v) => setFilter({ environment: v })}
        optionTitle={environmentTitle}
      />
      <Select
        label="product"
        value={filters.product}
        options={products}
        onChange={(v) => setFilter({ product: v })}
      />
      <Select label="tag" value={filters.tag} options={tags} onChange={(v) => setFilter({ tag: v })} />
      {filtered && (
        <button
          type="button"
          onClick={clearFilters}
          className="rounded border border-[var(--color-border)] px-2 py-1 text-sm text-[var(--color-text-muted)] hover:bg-[var(--color-accent-muted)]"
        >
          clear filters
        </button>
      )}
      <span className="ml-auto text-xs text-[var(--color-text-muted)]">
        {runs.length} run(s) match current filters
      </span>
    </div>
  );
}
