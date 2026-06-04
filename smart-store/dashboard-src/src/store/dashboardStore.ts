// Single zustand store for everything per-store the dashboard renders.
// Components consume slices via small selectors; nothing in the components
// fetches on its own — that's the orchestrator hook's job.

import { create } from 'zustand';
import type {
    AnomaliesResponse, FunnelResponse, HealthResponse, HeatmapResponse,
    InsightsResponse, MetricsResponse, WSEvent,
} from '../api/types';

export type ConnState = 'idle' | 'connecting' | 'live' | 'reconnecting' | 'degraded' | 'error' | 'polling';

// Each REST slice carries the storeId of the store it was fetched for. Panels
// that read a slice can compare that tag against `activeStore` and gate
// rendering — this is what prevents Store-1 data from being shown when the
// user has just clicked over to Store-2.
//
// Why we don't blank slices on switch: it produced a noticeable empty-state
// flash while the new REST round-trip (~300-500ms) was in flight. By keeping
// the previous slices around but tagged, components can choose to either (a)
// show nothing while activeStore !== slice._source so the old data doesn't
// bleed, OR (b) show the previous data dimmed. We chose (a) for safety.

export interface Tagged<T> {
    storeId: string;
    data: T;
}

interface State {
    activeStore: string | null;
    inputStore: string;

    connState: ConnState;
    lastEventAt: number | null;     // epoch ms

    metrics: Tagged<MetricsResponse> | null;
    funnel: Tagged<FunnelResponse> | null;
    heatmap: Tagged<HeatmapResponse> | null;
    anomalies: Tagged<AnomaliesResponse> | null;
    insights: Tagged<InsightsResponse> | null;
    health: HealthResponse | null;

    events: WSEvent[];
    eventsStore: string | null;     // which store `events` belongs to

    setInputStore: (s: string) => void;
    setActiveStore: (s: string | null) => void;
    setConnState: (c: ConnState) => void;
    pushEvent: (e: WSEvent, storeId: string) => void;
    clearEvents: (storeId: string) => void;
    setMetrics: (storeId: string, m: MetricsResponse) => void;
    setFunnel: (storeId: string, f: FunnelResponse) => void;
    setHeatmap: (storeId: string, h: HeatmapResponse) => void;
    setAnomalies: (storeId: string, a: AnomaliesResponse) => void;
    setInsights: (storeId: string, i: InsightsResponse) => void;
    setHealth: (h: HealthResponse) => void;
}

export const useDashboardStore = create<State>((set) => ({
    activeStore: null,
    inputStore: 'STORE_BLR_001',

    connState: 'idle',
    lastEventAt: null,

    metrics: null,
    funnel: null,
    heatmap: null,
    anomalies: null,
    insights: null,
    health: null,

    events: [],
    eventsStore: null,

    setInputStore: (s) => set({ inputStore: s }),
    setActiveStore: (s) => set({ activeStore: s }),
    setConnState:   (c) => set({ connState: c }),

    pushEvent: (e, storeId) => set((st) => {
        // If the event arrived for a store other than the one our event log
        // currently tracks, drop it — we don't want Store-1 events showing up
        // in Store-2's live stream after a switch.
        if (st.eventsStore && st.eventsStore !== storeId) return st;
        return {
            events: [e, ...st.events].slice(0, 80),
            eventsStore: storeId,
            lastEventAt: Date.now(),
        };
    }),

    clearEvents: (storeId) => set({ events: [], eventsStore: storeId, lastEventAt: null }),

    setMetrics:   (storeId, m) => set({ metrics:   { storeId, data: m } }),
    setFunnel:    (storeId, f) => set({ funnel:    { storeId, data: f } }),
    setHeatmap:   (storeId, h) => set({ heatmap:   { storeId, data: h } }),
    setAnomalies: (storeId, a) => set({ anomalies: { storeId, data: a } }),
    setInsights:  (storeId, i) => set({ insights:  { storeId, data: i } }),
    setHealth:    (h) => set({ health: h }),
}));

// Convenience selector: read a tagged slice but unwrap to the underlying data
// only when its storeId matches `activeStore`. Otherwise null.
export function useActiveSlice<T>(
    pick: (s: State) => Tagged<T> | null,
): T | null {
    return useDashboardStore((s) => {
        const slice = pick(s);
        if (!slice) return null;
        if (s.activeStore && slice.storeId !== s.activeStore) return null;
        return slice.data;
    });
}
