import React from 'react';
import { useActiveSlice } from '../store/dashboardStore';

export const AnomalyFeed: React.FC = () => {
    const a = useActiveSlice((s) => s.anomalies);
    const items = a?.anomalies ?? [];
    return (
        <div className="card">
            <div className="card-label">What's going wrong right now</div>
            {items.length === 0 ? (
                <div className="anom INFO" style={{ marginTop: 10 }}>
                    <div className="head">All clear</div>
                    <div className="body">No active anomalies.</div>
                </div>
            ) : (
                <div style={{ marginTop: 10 }}>
                    {items.map((x, i) => (
                        <div className={`anom ${x.severity}`} key={`${x.code}-${i}`}>
                            <div className="head">
                                <span>{x.code}</span>
                                <span style={{ color: 'var(--text-3)', fontWeight: 400, fontSize: 11 }}>· {x.severity}</span>
                            </div>
                            <div className="body">{x.detail}</div>
                            <div className="sa">{x.suggested_action}</div>
                        </div>
                    ))}
                </div>
            )}
        </div>
    );
};
