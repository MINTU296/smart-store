// Single orchestrator: fetches REST data on mount, polls aggressively while
// the WebSocket is not live, and treats the WS purely as a low-latency
// supplementary channel. The dashboard renders correctly even if the WS
// never opens.
//
// Key design: REST-first, slice-tagged.
//   - On mount and on every store change, fire all REST endpoints in parallel
//     so KPI tiles, funnel, heatmap, anomalies, insights, health all render
//     within ~200ms regardless of WS state.
//   - We set activeStore IMMEDIATELY on switch (so panels know the new
//     identity) but we do NOT clear the previous slices — they stay in memory
//     tagged with the prior storeId so the empty-state flash is invisible.
//     Panels read via `useActiveSlice`, which only returns a slice's data
//     when its storeId matches activeStore — so nothing bleeds across.
//   - As soon as the first REST round-trip lands for the new store, the
//     panels render fresh data. Until then they show their loading state for
//     ~100-300ms.
//   - A single 5s tick drives REST polling. When the WS is `live`, we throttle
//     to every 3rd tick (~15s) since the WS already pushes snapshot
//     heartbeats. When the WS is not live, every tick fires (5s cadence).
//   - WebSocket open is on a 4s timer: if it doesn't open by then, the
//     connection pill softens to `polling` rather than alarming the user.
//   - The "metrics" REST call is fired separately and ahead of the others —
//     that response drives the headline KPI tiles, the panels users notice
//     first. The heavier `/insights` payload arrives a beat later.

import { useEffect, useRef } from 'react';
import { api } from '../api/client';
import { openLiveSocket, type WSHandle } from '../api/ws';
import { useDashboardStore } from '../store/dashboardStore';

const SLOW_POLL_MS = 5_000;
const RECONNECT_MS = 2_000;
const WS_OPEN_TIMEOUT_MS = 4_000;

export function useStoreIntelligence(targetStore: string) {
    const setConnState = useDashboardStore((s) => s.setConnState);
    const setActive = useDashboardStore((s) => s.setActiveStore);
    const setMetrics = useDashboardStore((s) => s.setMetrics);
    const setFunnel = useDashboardStore((s) => s.setFunnel);
    const setHeatmap = useDashboardStore((s) => s.setHeatmap);
    const setAnomalies = useDashboardStore((s) => s.setAnomalies);
    const setInsights = useDashboardStore((s) => s.setInsights);
    const setHealth = useDashboardStore((s) => s.setHealth);
    const pushEvent = useDashboardStore((s) => s.pushEvent);
    const clearEvents = useDashboardStore((s) => s.clearEvents);

    const wsRef = useRef<WSHandle | null>(null);
    const targetRef = useRef<string>(targetStore);
    targetRef.current = targetStore;

    useEffect(() => {
        let pollTimer: number | null = null;
        let reconnectTimer: number | null = null;
        let openTimer: number | null = null;
        let pollTick = 0;
        let aborted = false;

        const isStillActive = (storeId: string) => !aborted && targetRef.current === storeId;

        // Race the metrics call ahead of the heavier endpoints — the headline
        // tiles are what the user notices first, and metrics is the cheapest
        // payload. Within ~100ms of switching stores those tiles repopulate.
        const refreshMetrics = async (storeId: string) => {
            try {
                const m = await api.metrics(storeId);
                if (!isStillActive(storeId)) return;
                if (m) setMetrics(storeId, m);
            } catch { /* silent */ }
        };

        const refreshAll = async (storeId: string) => {
            try {
                const [metrics, funnel, heat, anom, ins] = await Promise.all([
                    api.metrics(storeId).catch(() => null),
                    api.funnel(storeId).catch(() => null),
                    api.heatmap(storeId).catch(() => null),
                    api.anomalies(storeId).catch(() => null),
                    api.insights(storeId).catch(() => null),
                ]);
                if (!isStillActive(storeId)) return;
                if (metrics) setMetrics(storeId, metrics);
                if (funnel) setFunnel(storeId, funnel);
                if (heat) setHeatmap(storeId, heat);
                if (anom) setAnomalies(storeId, anom);
                if (ins) setInsights(storeId, ins);
            } catch {
                /* silent */
            }
        };

        const refreshHealth = async () => {
            try {
                const h = await api.health();
                if (!aborted) setHealth(h);
            } catch { /* silent */ }
        };

        const connect = (storeId: string) => {
            if (wsRef.current) {
                wsRef.current.close();
                wsRef.current = null;
            }
            // Don't wipe REST data here — the REST poll has already populated
            // (or is about to) and we want the dashboard to stay rendered
            // across WS flaps. Reset only happens on store CHANGE, in the
            // outer effect.
            setConnState('connecting');

            // If WS hasn't opened in 4s, soften the pill to "polling" — REST
            // is still keeping the data fresh, the user shouldn't be alarmed.
            if (openTimer) window.clearTimeout(openTimer);
            openTimer = window.setTimeout(() => {
                if (isStillActive(storeId)) {
                    const cur = useDashboardStore.getState().connState;
                    if (cur !== 'live') setConnState('polling');
                }
            }, WS_OPEN_TIMEOUT_MS);

            const handle = openLiveSocket(storeId, {
                onOpen: () => {
                    if (!isStillActive(storeId)) return;
                    setConnState('live');
                    if (openTimer) { window.clearTimeout(openTimer); openTimer = null; }
                    refreshAll(storeId);
                    refreshHealth();
                },
                onClose: (intentional) => {
                    if (intentional || !isStillActive(storeId)) return;
                    // Soften the message: REST poll is keeping data fresh.
                    setConnState('polling');
                    reconnectTimer = window.setTimeout(() => {
                        if (isStillActive(storeId)) connect(storeId);
                    }, RECONNECT_MS);
                },
                onError: () => {
                    // Don't escalate; the open-timeout handler will move us to
                    // 'polling'. If the WS truly never connects, the user
                    // still sees fresh REST data with an honest amber pill.
                },
                onFrame: (frame) => {
                    if (!isStillActive(storeId)) return;
                    if (frame.type === 'snapshot') {
                        setMetrics(storeId, frame.data);
                    } else if (frame.type === 'event') {
                        pushEvent(frame.data, storeId);
                    } else if (frame.type === 'degraded') {
                        setConnState('degraded');
                    }
                },
                isStillActive: () => isStillActive(storeId),
            });
            wsRef.current = handle;
        };

        // Mark the new active store IMMEDIATELY on switch so panels gate on
        // the new identity. We do NOT clear the previous REST slices —
        // panels gate their render on slice.storeId === activeStore (via
        // useActiveSlice), so the old data is invisible without a blank
        // flash while REST is in flight.
        setActive(targetStore);
        clearEvents(targetStore);

        // Headline KPI tiles first — they're what the user looks at after
        // clicking switch — then the heavier endpoints in one Promise.all.
        refreshMetrics(targetStore);
        refreshAll(targetStore);
        refreshHealth();
        connect(targetStore);

        // Single timer drives both fast (5s while not live) and slow (15s
        // while live) polling.
        pollTimer = window.setInterval(() => {
            if (aborted || !targetRef.current) return;
            pollTick += 1;
            const live = useDashboardStore.getState().connState === 'live';
            const shouldPoll = !live || pollTick % 3 === 0;
            if (shouldPoll) {
                refreshAll(targetRef.current);
                refreshHealth();
            }
        }, SLOW_POLL_MS);

        return () => {
            aborted = true;
            if (pollTimer) window.clearInterval(pollTimer);
            if (reconnectTimer) window.clearTimeout(reconnectTimer);
            if (openTimer) window.clearTimeout(openTimer);
            if (wsRef.current) {
                wsRef.current.close();
                wsRef.current = null;
            }
        };
    }, [
        targetStore,
        setActive, setConnState,
        setMetrics, setFunnel, setHeatmap, setAnomalies, setInsights, setHealth,
        pushEvent, clearEvents,
    ]);
}
