// Typed WebSocket helper.
//
// Critical: ports the bug-fix from the vanilla dashboard. Without these
// guards, switching stores caused (a) the previous socket's onclose handler
// to schedule an orphan reconnect that fights the new socket, and (b) frames
// from a stale socket bleeding into the new store's UI.
//
//   - intentionalClose flag — distinguishes user-driven close (Switch button)
//     from network drops (need to auto-reconnect).
//   - per-socket storeId capture — late-firing handlers know which store
//     they belonged to and can no-op cleanly.
//   - onActiveStoreMismatch — the orchestrator hook supplies the currently
//     active store and we drop frames that don't match.

import type { WSFrame } from './types';

export interface WSHandlers {
    onOpen?:    () => void;
    onFrame?:   (frame: WSFrame) => void;
    onClose?:   (intentional: boolean) => void;
    onError?:   () => void;
    isStillActive?: () => boolean;   // returns true if this socket's store is still the user's choice
}

export interface WSHandle {
    close: () => void;
    storeId: string;
}

export function openLiveSocket(storeId: string, handlers: WSHandlers): WSHandle {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const url = `${proto}://${location.host}/ws/${encodeURIComponent(storeId)}`;
    const ws = new WebSocket(url);
    let intentional = false;

    ws.onopen = () => handlers.onOpen?.();

    ws.onmessage = (msg) => {
        if (handlers.isStillActive && !handlers.isStillActive()) return;
        let frame: WSFrame;
        try { frame = JSON.parse(msg.data) as WSFrame; } catch { return; }
        handlers.onFrame?.(frame);
    };

    ws.onerror = () => {
        if (handlers.isStillActive && !handlers.isStillActive()) return;
        handlers.onError?.();
    };

    ws.onclose = () => {
        handlers.onClose?.(intentional);
    };

    return {
        storeId,
        close: () => {
            intentional = true;
            try { ws.close(); } catch { /* swallow */ }
        },
    };
}
