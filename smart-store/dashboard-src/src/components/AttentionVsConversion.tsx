import React from 'react';
import {
    CartesianGrid, ResponsiveContainer, Scatter, ScatterChart, Tooltip, XAxis, YAxis, ZAxis, Cell,
    ReferenceLine,
} from 'recharts';
import { useActiveSlice } from '../store/dashboardStore';

export const AttentionVsConversion: React.FC = () => {
    const insights = useActiveSlice((s) => s.insights);
    const zones = insights?.zone_attention_vs_conversion ?? [];

    if (!zones.length) {
        return (
            <div className="card">
                <div className="card-label">Attention vs conversion by zone</div>
                <div className="card-sub" style={{ marginTop: 6 }}>No zone data yet.</div>
            </div>
        );
    }

    const data = zones.map((z) => ({
        ...z,
        x: z.attention_score,
        y: z.conversion_rate * 100,
        z: 80,
    }));
    const offender = zones.find((z) => z.flag === 'high_attention_low_conv') ?? null;

    return (
        <div className="card">
            <div className="card-label">Attention vs conversion by zone</div>
            <div style={{ width: '100%', height: 220, marginTop: 6 }}>
                <ResponsiveContainer>
                    <ScatterChart margin={{ top: 8, right: 18, left: 0, bottom: 4 }}>
                        <CartesianGrid stroke="rgba(15,23,42,0.05)" />
                        <XAxis type="number" dataKey="x" name="Attention" domain={[0, 100]}
                               stroke="#94a3b8" fontSize={11} tickLine={false} />
                        <YAxis type="number" dataKey="y" name="Conversion %" domain={[0, 100]}
                               stroke="#94a3b8" fontSize={11} tickLine={false} unit="%" />
                        <ZAxis dataKey="z" range={[40, 240]} />
                        <ReferenceLine x={70} stroke="rgba(99,102,241,0.25)" strokeDasharray="4 4" />
                        <Tooltip
                            contentStyle={{ background: '#fff', border: '1px solid rgba(15,23,42,0.06)', borderRadius: 12, fontSize: 12 }}
                            cursor={{ stroke: '#a5b4fc', strokeDasharray: '4 4' }}
                            formatter={(value: any, name: any, p: any) => {
                                if (name === 'x') return [(value as number).toFixed(0), 'Attention'];
                                if (name === 'y') return [`${(value as number).toFixed(1)}%`, 'Conversion'];
                                return [value, name];
                            }}
                            labelFormatter={(_l, items) => {
                                const zid = (items?.[0]?.payload as any)?.zone_id;
                                return zid ? `Zone · ${zid}` : '';
                            }}
                        />
                        <Scatter data={data}>
                            {data.map((d, i) => (
                                <Cell
                                    key={i}
                                    fill={d.flag === 'high_attention_low_conv' ? '#ef4444' : '#6366f1'}
                                    fillOpacity={0.75}
                                />
                            ))}
                        </Scatter>
                    </ScatterChart>
                </ResponsiveContainer>
            </div>
            {offender && (
                <div className="callout" style={{ marginTop: 12, background: 'rgba(239,68,68,0.06)', color: 'var(--bad)', borderColor: 'rgba(239,68,68,0.15)' }}>
                    Worst offender: <strong>{offender.zone_id}</strong> — high attention ({offender.attention_score.toFixed(0)}) but only {(offender.conversion_rate * 100).toFixed(1)}% convert.
                </div>
            )}
        </div>
    );
};
