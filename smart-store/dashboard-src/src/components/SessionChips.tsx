import React from 'react';
import { useActiveSlice } from '../store/dashboardStore';

const fmtSec = (s: number) => {
    if (!s || s <= 0) return '—';
    if (s < 60) return `${s.toFixed(0)}s`;
    const m = Math.floor(s / 60);
    const r = Math.round(s % 60);
    return `${m}m ${r}s`;
};

export const SessionChips: React.FC = () => {
    const insights = useActiveSlice((s) => s.insights);
    const c = insights?.session_chips;
    return (
        <div className="card">
            <div className="card-label">Session insights</div>
            <div className="chips">
                <div className="chip">
                    <div className="lab">Re-entry rate</div>
                    <div className="val">{c ? `${(c.reentry_rate * 100).toFixed(1)}%` : '—'}</div>
                </div>
                <div className="chip">
                    <div className="lab">Avg zones / trip</div>
                    <div className="val">{c ? c.avg_zones_per_trip.toFixed(1) : '—'}</div>
                </div>
                <div className="chip">
                    <div className="lab">Time to first zone</div>
                    <div className="val">{c ? fmtSec(c.time_to_first_zone_s) : '—'}</div>
                </div>
            </div>
        </div>
    );
};
