import React from 'react';
import { useActiveSlice } from '../store/dashboardStore';

// Pastel quiet→hot scale on a light theme. Score is 0-100. We map low scores to
// a cool teal background and high scores to a warm pink/violet.
const cellGradient = (score: number): { from: string; to: string } => {
    const t = Math.min(100, Math.max(0, score)) / 100;
    // Cool: #e0f2fe (sky-100) → Warm: #fde2e7 (pink-100). For the secondary
    // gradient stop we blend through indigo.
    const from = `rgba(${224 + t * (253 - 224)}, ${242 - t * (242 - 226)}, ${254 - t * (254 - 231)}, 1)`;
    const to   = `rgba(${221 - t * (221 - 252)}, ${214 + t * (210 - 214)}, ${254 - t * (254 - 220)}, 1)`;
    return { from, to };
};

export const ZoneHeatmap: React.FC = () => {
    const heatmap = useActiveSlice((s) => s.heatmap);
    const insights = useActiveSlice((s) => s.insights);

    const zones = heatmap?.zones ? [...heatmap.zones].sort((a, b) => b.score - a.score) : [];
    const busiest = zones[0];
    const quietest = zones[zones.length - 1];

    if (!zones.length) {
        return (
            <div className="card pastel">
                <div className="card-label">Where people go</div>
                <div className="card-sub" style={{ marginTop: 6 }}>
                    No zone activity yet.{heatmap?.data_confidence === 'low' && ` Low confidence (${heatmap.sessions_in_window} sessions).`}
                </div>
            </div>
        );
    }

    return (
        <div className="card pastel">
            <div className="card-label">Where people go</div>
            <div className="heatgrid">
                {zones.map((z) => {
                    const g = cellGradient(z.score);
                    return (
                        <div
                            className="heatcell"
                            key={z.zone_id}
                            style={{
                                ['--cell-from' as any]: g.from,
                                ['--cell-to' as any]: g.to,
                            }}
                            title={`${z.zone_id} · score ${z.score.toFixed(0)}`}
                        >
                            <div className="zname">{z.zone_id}</div>
                            <div className="ssub">
                                {z.visits} visits · {(z.avg_dwell_ms / 1000).toFixed(0)}s
                            </div>
                            <div className="ssub">
                                <strong>{z.score.toFixed(0)}</strong> / 100
                            </div>
                        </div>
                    );
                })}
            </div>
            {busiest && quietest && busiest.zone_id !== quietest.zone_id && (
                <div className="callout">
                    <strong>{insights?.busiest_zone ?? busiest.zone_id}</strong> is busiest right now.{' '}
                    <strong>{insights?.quietest_zone ?? quietest.zone_id}</strong> is quietest — consider promo or staff redeploy.
                </div>
            )}
        </div>
    );
};
