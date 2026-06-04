import React from 'react';
import { useActiveSlice } from '../store/dashboardStore';

export const ConversionFunnel: React.FC = () => {
    const funnel = useActiveSlice((s) => s.funnel);
    if (!funnel || !funnel.stages.length) {
        return (
            <div className="card">
                <div className="card-label">Where you lose customers</div>
                <div className="card-sub" style={{ marginTop: 6 }}>No funnel data yet.</div>
            </div>
        );
    }
    const top = funnel.stages[0]?.count || 1;
    return (
        <div className="card">
            <div className="card-label">Where you lose customers</div>
            <div className="funnel">
                {funnel.stages.map((s, i) => {
                    const fill = top > 0 ? (s.count / top) * 100 : 0;
                    return (
                        <React.Fragment key={s.name}>
                            <div className="funnel-row">
                                <div className="label">{s.name}</div>
                                <div className="funnel-bar">
                                    <div className="fill" style={{ width: `${fill}%` }} />
                                </div>
                                <div className="count">{s.count}</div>
                            </div>
                            {i < funnel.stages.length - 1 && (
                                <div className="dropoff">↓ drop-off {funnel.stages[i + 1].drop_off_pct.toFixed(1)}%</div>
                            )}
                        </React.Fragment>
                    );
                })}
            </div>
        </div>
    );
};
