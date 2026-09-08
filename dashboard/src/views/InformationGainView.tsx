import { useMemo, useState } from 'react';
import { useDashboard } from '../lib/dashboardCtx';
import type { ChannelGain } from '../lib/types';

const WIDTH = 860;
const BAR_HEIGHT = 22;
const GAP = 26;
const LABEL_WIDTH = 40;
const AXIS_HEIGHT = 24;

/** The information gain chart (plan.md section 11): the centrepiece. Horizontal diverging bars,
 * sorted by gain, an emphasised zero line, negative-gain (harmful) channels rendered in the
 * "negative" colour so they read as harmful at a glance, each bar carrying its n and a strip
 * plot of per-bug deltas so the spread behind the mean is visible. Facetable by environment and
 * model; a toggle switches pooled vs per-environment. */
export function InformationGainView() {
  const { doc, informationGain } = useDashboard();
  const [facet, setFacet] = useState<'pooled' | 'environment' | 'model'>('pooled');
  const [facetValue, setFacetValue] = useState<string>(doc.campaign.environments[0] ?? '');

  const channels: ChannelGain[] = useMemo(() => {
    if (facet === 'pooled') return informationGain.pooled;
    if (facet === 'environment') return informationGain.by_environment[facetValue] ?? [];
    return informationGain.by_model[facetValue] ?? [];
  }, [facet, facetValue, informationGain]);

  const sorted = [...channels].sort((a, b) => b.mean - a.mean);
  const maxAbs = Math.max(0.01, ...sorted.map((c) => Math.max(...Object.values(c.per_bug).map(Math.abs), Math.abs(c.mean))));
  const plotWidth = WIDTH - LABEL_WIDTH;
  const zeroX = LABEL_WIDTH + plotWidth / 2;
  const scale = (v: number) => (v / maxAbs) * (plotWidth / 2 - 8);

  const height = AXIS_HEIGHT + sorted.length * (BAR_HEIGHT + GAP);

  const facetOptions = facet === 'environment' ? doc.campaign.environments : doc.campaign.models;

  return (
    <div className="p-4">
      <h2 className="mb-1 text-lg font-semibold">Information gain</h2>
      <p className="mb-3 text-xs text-[var(--color-text-muted)]">
        score(full) − score(leave-one-out) per channel, meaned per bug then over bugs. A bar to
        the left of the zero line means the channel <strong>hurt</strong> the model — a harmful
        channel is the most important thing this chart can show.
      </p>
      <div className="mb-3 flex items-center gap-3 text-sm">
        <label className="flex items-center gap-1.5">
          <span className="text-[var(--color-text-muted)]">facet</span>
          <select
            className="rounded border border-[var(--color-border)] bg-[var(--color-surface)] px-2 py-1"
            value={facet}
            onChange={(e) => {
              const v = e.target.value as typeof facet;
              setFacet(v);
              if (v === 'environment') setFacetValue(doc.campaign.environments[0] ?? '');
              if (v === 'model') setFacetValue(doc.campaign.models[0] ?? '');
            }}
          >
            <option value="pooled">pooled</option>
            <option value="environment">by environment</option>
            <option value="model">by model</option>
          </select>
        </label>
        {facet !== 'pooled' && (
          <select
            className="rounded border border-[var(--color-border)] bg-[var(--color-surface)] px-2 py-1"
            value={facetValue}
            onChange={(e) => setFacetValue(e.target.value)}
          >
            {facetOptions.map((o) => (
              <option key={o} value={o}>
                {o}
              </option>
            ))}
          </select>
        )}
      </div>

      {sorted.length === 0 ? (
        <p className="text-[var(--color-text-muted)]">No channel gain data for this facet.</p>
      ) : (
        <svg width={WIDTH} height={height} role="img" aria-label="information gain per channel">
          {/* emphasised zero line */}
          <line x1={zeroX} y1={0} x2={zeroX} y2={height} stroke="var(--color-text)" strokeWidth={2} />
          {sorted.map((c, i) => {
            const y = AXIS_HEIGHT + i * (BAR_HEIGHT + GAP);
            const barW = Math.abs(scale(c.mean));
            const barX = c.mean >= 0 ? zeroX : zeroX - barW;
            const color = c.mean < 0 ? 'var(--color-negative)' : 'var(--color-positive)';
            const perBugValues = Object.values(c.per_bug);
            return (
              <g key={c.channel} opacity={c.low_confidence ? 0.6 : 1}>
                <text x={LABEL_WIDTH} y={y - 6} fontSize={12} fontWeight={600} fill="var(--color-text)">
                  {c.channel}
                  {c.mean < 0 ? ' \u2014 harmful' : ''}
                </text>
                <rect x={barX} y={y} width={Math.max(barW, 1)} height={BAR_HEIGHT} fill={color} rx={2} />
                {/* strip plot of per-bug deltas behind the bar */}
                {perBugValues.map((v, j) => (
                  <circle
                    key={j}
                    cx={zeroX + scale(v)}
                    cy={y + BAR_HEIGHT / 2}
                    r={2.5}
                    fill="var(--color-text)"
                    opacity={0.5}
                  />
                ))}
                <text
                  x={c.mean >= 0 ? barX + barW + 6 : zeroX + 6}
                  y={y + BAR_HEIGHT / 2 + 4}
                  fontSize={11}
                  textAnchor="start"
                  fontWeight={c.mean < 0 ? 700 : 400}
                  fill={c.mean < 0 ? 'var(--color-negative)' : 'var(--color-text)'}
                >
                  {c.mean >= 0 ? '+' : ''}
                  {c.mean.toFixed(3)} (n={c.n}
                  {c.low_confidence ? ', low confidence' : ''})
                </text>
              </g>
            );
          })}
        </svg>
      )}
    </div>
  );
}
