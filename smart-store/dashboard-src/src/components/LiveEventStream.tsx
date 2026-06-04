import React from 'react';
import { useDashboardStore } from '../store/dashboardStore';

const fmtTime = (iso: string) => {
    try { return new Date(iso).toLocaleTimeString(); }
    catch { return iso; }
};

export const LiveEventStream: React.FC = () => {
    const events = useDashboardStore((s) => s.events);
    return (
        <div className="card">
            <div className="card-label">Live event stream</div>
            <div className="stream">
                {events.length === 0 ? (
                    <div className="row"><span className="badge">idle</span><span style={{ color: 'var(--text-3)' }}>waiting for events…</span></div>
                ) : (
                    events.map((e) => (
                        <div className="row" key={e.event_id}>
                            <span className="badge">{e.event_type}</span>
                            <span style={{ color: 'var(--text-2)' }}>{fmtTime(e.ts)}</span>
                            <span style={{ color: 'var(--text-3)' }}>{e.visitor_id}</span>
                            <span>{e.zone_id ?? ''}</span>
                            {e.queue_depth != null && (
                                <span style={{ color: 'var(--warn)' }}>q={e.queue_depth}</span>
                            )}
                        </div>
                    ))
                )}
            </div>
        </div>
    );
};
