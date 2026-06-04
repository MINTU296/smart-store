// Same-origin fetch helpers. The dashboard is served from /dashboard/ and the
// API endpoints live at /stores/* and /health, so we use relative paths. In
// `npm run dev`, vite proxies these to localhost:8000 (see vite.config.ts).

import type {
    AnomaliesResponse, FunnelResponse, HealthResponse, HeatmapResponse,
    InsightsResponse, MetricsResponse,
} from './types';

async function get<T>(path: string): Promise<T> {
    const r = await fetch(path);
    if (!r.ok) throw new Error(`${path} → HTTP ${r.status}`);
    return r.json() as Promise<T>;
}

export const api = {
    metrics:   (id: string) => get<MetricsResponse>(`/stores/${encodeURIComponent(id)}/metrics`),
    funnel:    (id: string) => get<FunnelResponse>(`/stores/${encodeURIComponent(id)}/funnel`),
    heatmap:   (id: string) => get<HeatmapResponse>(`/stores/${encodeURIComponent(id)}/heatmap`),
    anomalies: (id: string) => get<AnomaliesResponse>(`/stores/${encodeURIComponent(id)}/anomalies`),
    health:    () => get<HealthResponse>('/health'),
    insights:  (id: string, hours = 24) =>
        get<InsightsResponse>(`/stores/${encodeURIComponent(id)}/insights?window_hours=${hours}`),
};
