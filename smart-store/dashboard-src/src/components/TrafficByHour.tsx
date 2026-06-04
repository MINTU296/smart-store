import React from 'react';
import {
    Bar, CartesianGrid, ComposedChart, Line, ResponsiveContainer, Tooltip, XAxis, YAxis,
    Cell,
} from 'recharts';
import { useActiveSlice } from '../store/dashboardStore';

const fmtHour = (iso: string) => {
    try { return new Date(iso).toLocaleTimeString([], { hour: '2-digit' }); }
    catch { return iso.slice(11, 13) + ':00'; }
};

export const TrafficByHour: React.FC = () => {
    const insights = useActiveSlice((s) => s.insights);
    const buckets = insights?.traffic_by_hour ?? [];

    if (!buckets.length) {
        return (
            <div className="card">
                <div className="card-label">Traffic by hour</div>
                <div className="card-sub" style={{ marginTop: 6 }}>No traffic data in this window.</div>
            </div>
        );
    }

    const data = buckets.map((b) => ({
        ...b,
        hourLabel: fmtHour(b.hour),
        conversionPct: b.conversion_rate * 100,
    }));
    const peak = buckets.find((b) => b.is_peak);
    const peakConv = peak?.conversion_rate ?? 0;
    const allConvs = buckets.map((b) => b.conversion_rate).filter((v) => v >= 0);
    const medianConv = allConvs.length
        ? [...allConvs].sort((a, b) => a - b)[Math.floor(allConvs.length / 2)]
        : 0;
    const peakDrops = peak && peakConv < medianConv;

    return (
        <div className="card">
            <div className="card-label">Traffic by hour</div>
            <div style={{ width: '100%', height: 220, marginTop: 6 }}>
                <ResponsiveContainer>
                    <ComposedChart data={data} margin={{ top: 8, right: 18, left: 0, bottom: 4 }}>
                        <CartesianGrid stroke="rgba(15,23,42,0.05)" />
                        <XAxis dataKey="hourLabel" stroke="#94a3b8" fontSize={11} tickLine={false} />
                        <YAxis yAxisId="left" stroke="#94a3b8" fontSize={11} tickLine={false} axisLine={false} />
                        <YAxis yAxisId="right" orientation="right" stroke="#94a3b8" fontSize={11}
                               tickLine={false} axisLine={false}
                               domain={[0, 100]} unit="%" />
                        <Tooltip
                            contentStyle={{ background: '#fff', border: '1px solid rgba(15,23,42,0.06)', borderRadius: 12, fontSize: 12 }}
                            formatter={(value: any, name: any) => {
                                if (name === 'Conversion') return [`${(value as number).toFixed(1)}%`, 'Conversion'];
                                return [value, name];
                            }}
                        />
                        <Bar yAxisId="left" dataKey="entries" name="Entries" radius={[6, 6, 0, 0]}>
                            {data.map((d, i) => (
                                <Cell key={i} fill={d.is_peak ? '#6366f1' : '#a5b4fc'} />
                            ))}
                        </Bar>
                        <Line yAxisId="right" type="monotone" dataKey="conversionPct"
                              stroke="#06b6d4" strokeWidth={2} dot={false} name="Conversion" />
                    </ComposedChart>
                </ResponsiveContainer>
            </div>
            {peak && (
                <div className="callout" style={{ marginTop: 12 }}>
                    Peak hour: <strong>{fmtHour(peak.hour)}</strong> with {peak.entries} entries.
                    {peakDrops ? (
                        <> Conversion drops to <strong>{(peakConv * 100).toFixed(1)}%</strong> when you're busiest — likely under-staffed at peak.</>
                    ) : (
                        <> Conversion at peak: {(peakConv * 100).toFixed(1)}%.</>
                    )}
                </div>
            )}
        </div>
    );
};
