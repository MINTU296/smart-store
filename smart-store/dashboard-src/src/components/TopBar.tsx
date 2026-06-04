import React, { useEffect, useState } from 'react';
import { useDashboardStore, useActiveSlice } from '../store/dashboardStore';

interface Props {
    onSwitch: (storeId: string) => void;
}

const FALLBACK_STORES = ['STORE_BLR_001', 'STORE_BLR_002'];

const dotClass = (last: string | null, stale: boolean): 'green' | 'amber' | 'red' => {
    if (!last) return 'red';
    const ageMs = Date.now() - new Date(last).getTime();
    if (stale) return 'red';
    if (ageMs > 2 * 60_000) return 'amber';
    return 'green';
};

const fmtAge = (last: string | null): string => {
    if (!last) return 'no events';
    const sec = Math.max(0, Math.floor((Date.now() - new Date(last).getTime()) / 1000));
    if (sec < 60) return `${sec}s ago`;
    const m = Math.floor(sec / 60);
    if (m < 60) return `${m}m ago`;
    const h = Math.floor(m / 60);
    return `${h}h ago`;
};

export const TopBar: React.FC<Props> = ({ onSwitch }) => {
    const activeStore = useDashboardStore((s) => s.activeStore);
    const connState   = useDashboardStore((s) => s.connState);
    const insights    = useActiveSlice((s) => s.insights);
    const lastEventAt = useDashboardStore((s) => s.lastEventAt);
    const health      = useDashboardStore((s) => s.health);

    // Re-render every 5s so "X seconds ago" stays fresh.
    const [, setTick] = useState(0);
    useEffect(() => {
        const t = window.setInterval(() => setTick((n) => n + 1), 5000);
        return () => window.clearInterval(t);
    }, []);

    const cameras = insights?.cameras ?? [];
    const occupancy = insights?.occupancy?.current ?? 0;

    // Per-role rollup for the camera dots.
    const byRole: Record<string, { count: number; last: string | null; stale: boolean }> = {};
    for (const cam of cameras) {
        const r = cam.role || 'unknown';
        const cur = byRole[r] || { count: 0, last: null, stale: false };
        cur.count += 1;
        if (!cur.last || (cam.last_event_ts && cam.last_event_ts > cur.last)) {
            cur.last = cam.last_event_ts;
        }
        cur.stale = cur.stale || cam.stale;
        byRole[r] = cur;
    }

    const lastSeenIso: string | null = (() => {
        if (lastEventAt) return new Date(lastEventAt).toISOString();
        return cameras.reduce<string | null>(
            (acc, c) => (c.last_event_ts && (!acc || c.last_event_ts > acc)) ? c.last_event_ts : acc,
            null,
        );
    })();
    const stale = cameras.length > 0 && cameras.every((c) => c.stale);

    const connPill = (() => {
        switch (connState) {
            case 'live':         return <span className="pill good"><span className="dot green" />live{activeStore ? ` · ${activeStore}` : ''}</span>;
            case 'connecting':   return <span className="pill"><span className="dot amber" />connecting…</span>;
            case 'polling':      return <span className="pill"><span className="dot amber" />polling{activeStore ? ` · ${activeStore}` : ''}</span>;
            case 'reconnecting': return <span className="pill warn"><span className="dot amber" />reconnecting…</span>;
            case 'degraded':     return <span className="pill warn"><span className="dot amber" />degraded</span>;
            case 'error':        return <span className="pill bad"><span className="dot red" />error</span>;
            default:             return <span className="pill"><span className="dot amber" />idle</span>;
        }
    })();

    // Real <select> populated from /health.stores. Fallback list keeps the
    // picker functional during the brief window before /health returns.
    const knownIds = (health?.stores ?? []).map((s) => s.store_id);
    const options = knownIds.length ? knownIds : FALLBACK_STORES;
    const selected = activeStore && options.includes(activeStore) ? activeStore : options[0];

    return (
        <div className="topbar">
            <div className="brand">
                <h1>Apex Retail · Store Intelligence</h1>
                <div className="crumb">Live store-ops dashboard · {activeStore ?? '—'}</div>
            </div>

            <div className="cam-row">
                {(['entry', 'floor', 'billing'] as const).map((role) => {
                    const r = byRole[role];
                    if (!r) return (
                        <span className="cam" key={role}>
                            <span className="dot amber" /><span>{role}</span>
                        </span>
                    );
                    return (
                        <span className="cam" key={role}>
                            <span className={`dot ${dotClass(r.last, r.stale)}`} />
                            <span>{role}</span>
                            <span style={{ color: 'var(--text-3)' }}>· {fmtAge(r.last)}</span>
                        </span>
                    );
                })}
            </div>

            {stale && <span className="pill bad">STALE FEED · last event &gt; 10m</span>}
            {!stale && <span className="pill">last event · {fmtAge(lastSeenIso)}</span>}

            <span className="pill">
                <span style={{ fontWeight: 600 }}>{occupancy}</span>&nbsp;in store
            </span>

            <div className="spacer" />

            {connPill}

            <select
                className="store-input"
                value={selected}
                onChange={(e) => onSwitch(e.target.value)}
                aria-label="Select store"
            >
                {options.map((id) => (
                    <option key={id} value={id}>{id}</option>
                ))}
            </select>
        </div>
    );
};
