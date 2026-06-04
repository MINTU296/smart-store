import React from 'react';
import { useActiveSlice } from '../store/dashboardStore';
import { KpiCard } from './primitives/KpiCard';
import { DeltaBadge } from './primitives/DeltaBadge';

const pct = (v: number | null | undefined, digits = 1): string => {
    if (v === null || v === undefined || Number.isNaN(v)) return '—';
    return `${(v * 100).toFixed(digits)}%`;
};

const fmtMs = (ms: number | null | undefined): string => {
    if (!ms || ms <= 0) return '—';
    const s = Math.round(ms / 1000);
    if (s < 60) return `${s}s`;
    const m = Math.floor(s / 60);
    const r = s % 60;
    return `${m}:${String(r).padStart(2, '0')}`;
};

const trendArrow = (direction: string): string => {
    if (direction === 'growing') return '↗';
    if (direction === 'shrinking') return '↘';
    return '→';
};

export const HeadlineMetrics: React.FC = () => {
    const metrics = useActiveSlice((s) => s.metrics);
    const insights = useActiveSlice((s) => s.insights);

    const conv = metrics?.conversion_rate ?? null;
    const visitors = metrics?.unique_visitors ?? 0;
    const purchasing = metrics?.purchasing_visitors ?? 0;
    const queueDepth = metrics?.current_queue_depth ?? 0;
    const abandonment = metrics?.abandonment_rate ?? null;

    // Average dwell across all zones in metrics.avg_dwell_ms_per_zone
    const avgDwellMs = metrics?.avg_dwell_ms_per_zone
        ? Object.values(metrics.avg_dwell_ms_per_zone).filter((v) => v > 0).reduce((acc, v, _i, arr) => acc + v / arr.length, 0)
        : 0;

    const d = insights?.deltas;
    const trend = insights?.queue_trend?.direction ?? 'holding';

    return (
        <div className="grid row-headline">
            <KpiCard
                hero
                label="Conversion rate"
                value={pct(conv)}
                sub={`${purchasing} purchased of ${visitors}`}
                delta={d ? <DeltaBadge value={d.conversion_rate.delta_pp} unit="pp" /> : null}
            />
            <KpiCard
                pastel
                label="Unique visitors"
                value={visitors.toLocaleString()}
                sub="customers excl. staff"
                delta={d ? <DeltaBadge value={d.unique_visitors.delta_pct} unit="%" /> : null}
            />
            <KpiCard
                label="Avg dwell"
                value={fmtMs(avgDwellMs)}
                sub="across active zones"
                delta={d ? <DeltaBadge value={d.avg_dwell_ms.delta_pct} unit="%" /> : null}
            />
            <KpiCard
                label="Queue depth"
                value={
                    <span>
                        {queueDepth}{' '}
                        <span style={{ fontSize: 22, color: 'var(--accent-2)' }}>{trendArrow(trend)}</span>
                    </span>
                }
                sub={`billing · ${trend}`}
            />
            <KpiCard
                label="Abandonment"
                value={pct(abandonment)}
                sub="join → leave w/o pay"
                delta={d ? <DeltaBadge value={d.abandonment_rate.delta_pp} unit="pp" inverse /> : null}
            />
        </div>
    );
};
