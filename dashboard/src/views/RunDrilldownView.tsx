import { useMemo, useState } from 'react';
import { useDashboard } from '../lib/dashboardCtx';
import type { RunRow } from '../lib/types';

/** Run drilldown (plan.md section 11): filterable table down to the individual run — cell
 * coordinates, outcome, scores, the agent's answer, the judge's rationale, the ground truth, and
 * the netlog summary. No transcript content, ever — only the on-disk path, as text. */
export function RunDrilldownView() {
  const { runs } = useDashboard();
  const [selected, setSelected] = useState<RunRow | null>(null);
  const [search, setSearch] = useState('');

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return runs;
    return runs.filter(
      (r) =>
        r.run_id.toLowerCase().includes(q) ||
        r.bug_id.toLowerCase().includes(q) ||
        r.model.toLowerCase().includes(q),
    );
  }, [runs, search]);

  return (
    <div className="flex h-full gap-4 p-4">
      <div className="flex-1 overflow-auto rounded border border-[var(--color-border)] bg-[var(--color-surface)]">
        <div className="sticky top-0 border-b border-[var(--color-border)] bg-[var(--color-surface)] p-2">
          <input
            className="w-full rounded border border-[var(--color-border)] px-2 py-1 text-sm"
            placeholder="search run id / bug id / model"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>
        <table className="w-full text-sm">
          <thead className="border-b border-[var(--color-border)]">
            <tr>
              <th className="px-3 py-2 text-left">run</th>
              <th className="px-3 py-2 text-left">bug</th>
              <th className="px-3 py-2 text-left">env</th>
              <th className="px-3 py-2 text-left">channels</th>
              <th className="px-3 py-2 text-left">model</th>
              <th className="px-3 py-2 text-left">outcome</th>
              <th className="px-3 py-2 text-left">score</th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((r) => (
              <tr
                key={r.run_id}
                className={`cursor-pointer border-b border-[var(--color-border)] last:border-0 hover:bg-[var(--color-accent-muted)] ${selected?.run_id === r.run_id ? 'bg-[var(--color-accent-muted)]' : ''}`}
                onClick={() => setSelected(r)}
              >
                <td className="px-3 py-2 font-mono text-xs">{r.run_id}</td>
                <td className="px-3 py-2">{r.bug_id}</td>
                <td className="px-3 py-2">{r.environment}</td>
                <td className="px-3 py-2">{r.channel_set}</td>
                <td className="px-3 py-2">{r.model}</td>
                <td className="px-3 py-2">{r.outcome}</td>
                <td className="px-3 py-2">{r.score.toFixed(2)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="w-[420px] shrink-0 overflow-auto rounded border border-[var(--color-border)] bg-[var(--color-surface)] p-4 text-sm">
        {!selected ? (
          <p className="text-[var(--color-text-muted)]">Select a run to see its detail.</p>
        ) : (
          <div className="space-y-3">
            <div>
              <h3 className="font-semibold">{selected.run_id}</h3>
              <p className="text-xs text-[var(--color-text-muted)]">
                {selected.bug_id} · {selected.environment} · {selected.channel_set} · {selected.model} ·{' '}
                {selected.outcome}
              </p>
            </div>
            <div className="grid grid-cols-3 gap-2 text-xs">
              <div>score {selected.score.toFixed(2)}</div>
              <div>diagnosis {selected.diagnosis.toFixed(2)}</div>
              <div>localisation {selected.localisation.toFixed(2)}</div>
              <div>remedy {selected.remedy.toFixed(2)}</div>
              <div>file_f1 {selected.file_f1?.toFixed(2) ?? '—'}</div>
              <div>component_match {String(selected.component_match ?? '—')}</div>
            </div>
            <div>
              <h4 className="font-medium">Agent's root cause</h4>
              <p className="whitespace-pre-wrap text-[var(--color-text-muted)]">
                {selected.root_cause ?? '(no answer)'}
              </p>
            </div>
            <div>
              <h4 className="font-medium">Agent's proposed fix</h4>
              <p className="whitespace-pre-wrap text-[var(--color-text-muted)]">
                {selected.proposed_fix ?? '(no answer)'}
              </p>
            </div>
            <div>
              <h4 className="font-medium">Judge rationale — root cause</h4>
              <p className="whitespace-pre-wrap text-[var(--color-text-muted)]">
                {selected.root_cause_rationale ?? '—'}
              </p>
            </div>
            <div>
              <h4 className="font-medium">Judge rationale — fix</h4>
              <p className="whitespace-pre-wrap text-[var(--color-text-muted)]">{selected.fix_rationale ?? '—'}</p>
            </div>
            <div>
              <h4 className="font-medium">Ground truth root cause</h4>
              <p className="whitespace-pre-wrap text-[var(--color-text-muted)]">{selected.ground_truth_root_cause}</p>
            </div>
            <div>
              <h4 className="font-medium">Netlog</h4>
              <p className="text-[var(--color-text-muted)]">
                {selected.netlog.total_requests} requests, {selected.netlog.allowed} allowed,{' '}
                {selected.netlog.denied} denied, {selected.netlog.leak_attempts} leak attempt(s)
              </p>
            </div>
            <div>
              <h4 className="font-medium">Transcript</h4>
              <p className="break-all font-mono text-xs text-[var(--color-text-muted)]">{selected.transcript_path}</p>
              <p className="text-xs text-[var(--color-text-muted)]">Path only — transcripts never render here.</p>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
