import React from 'react';
import {
    Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis,
    Scatter, ComposedChart,
} from 'recharts';
import { useActiveSlice } from '../store/dashboardStore';

const fmtHour = (iso: string) => {
    try { return new Date(iso).toLocaleTimeString([], { hour: '2-digit' }); }
    catch { return iso.slice(11, 13) + ':00'; }
};

export const StaffVsCustomers: React.FC = () => {
    const insights = useActiveSlice((s) => s.insights);
    const buckets = insights?.staff_vs_customers ?? [];

    if (!buckets.length) {
        return (
            <div className="card">
                <div className="card-label">Staff vs customers</div>
                <div className="card-sub" style={{ marginTop: 6 }}>No headcount data in this window.</div>
            </div>
        );
    }

    const data = buckets.map((b) => ({
        hour: fmtHour(b.ts),
        staff: b.staff,
        customers: b.customers,
        understaffed: b.understaffed ? b.customers : null,
    }));

    const understaffedHours = buckets.filter((b) => b.understaffed).length;

    return (
        <div className="card">
            <div className="card-label">Staff vs customers</div>
            <div style={{ width: '100%', height: 220, marginTop: 6 }}>
                <ResponsiveContainer>
                    <ComposedChart data={data} margin={{ top: 8, right: 18, left: 0, bottom: 4 }}>
                        <defs>
                            <linearGradient id="cust" x1="0" y1="0" x2="0" y2="1">
                                <stop offset="0%" stopColor="#6366f1" stopOpacity={0.55} />
                                <stop offset="100%" stopColor="#6366f1" stopOpacity={0.05} />
                            </linearGradient>
                            <linearGradient id="staff" x1="0" y1="0" x2="0" y2="1">
                                <stop offset="0%" stopColor="#10b981" stopOpacity={0.45} />
                                <stop offset="100%" stopColor="#10b981" stopOpacity={0.05} />
                            </linearGradient>
                        </defs>
                        <CartesianGrid stroke="rgba(15,23,42,0.05)" />
                        <XAxis dataKey="hour" stroke="#94a3b8" fontSize={11} tickLine={false} />
                        <YAxis stroke="#94a3b8" fontSize={11} tickLine={false} axisLine={false} />
                        <Tooltip
                            contentStyle={{ background: '#fff', border: '1px solid rgba(15,23,42,0.06)', borderRadius: 12, fontSize: 12 }}
                        />
                        <Area type="monotone" dataKey="customers" stroke="#6366f1" fill="url(#cust)" name="Customers" />
                        <Area type="monotone" dataKey="staff" stroke="#10b981" fill="url(#staff)" name="Staff" />
                        <Scatter dataKey="understaffed" fill="#ef4444" name="Understaffed" />
                    </ComposedChart>
                </ResponsiveContainer>
            </div>
            {understaffedHours > 0 ? (
                <div className="callout" style={{ marginTop: 12, background: 'rgba(239,68,68,0.06)', color: 'var(--bad)', borderColor: 'rgba(239,68,68,0.15)' }}>
                    Understaffed in <strong>{understaffedHours}</strong> hour{understaffedHours === 1 ? '' : 's'} — customer:staff ratio &gt; 15:1.
                </div>
            ) : (
                <div className="callout" style={{ marginTop: 12 }}>
                    Staffing ratio looks healthy across the window.
                </div>
            )}
        </div>
    );
};
