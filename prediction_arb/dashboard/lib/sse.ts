'use client';

import { useEffect, useRef, useState } from 'react';

export interface SSEState {
  lastEvent: { type: string; data: unknown } | null;
  isConnected: boolean;
}

export function useSSE(url: string, token: string): SSEState {
  const [isConnected, setIsConnected] = useState(false);
  const [lastEvent, setLastEvent] = useState<{ type: string; data: unknown } | null>(null);
  const backoffRef = useRef(1000);
  const esRef = useRef<EventSource | null>(null);

  useEffect(() => {
    let cancelled = false;

    function connect() {
      if (cancelled) return;

      const es = new EventSource(`${url}?token=${encodeURIComponent(token)}`);
      esRef.current = es;

      es.onopen = () => {
        if (cancelled) { es.close(); return; }
        setIsConnected(true);
        backoffRef.current = 1000;
      };

      es.onerror = () => {
        setIsConnected(false);
        es.close();
        if (!cancelled) {
          const delay = backoffRef.current;
          backoffRef.current = Math.min(backoffRef.current * 2, 30000);
          setTimeout(connect, delay);
        }
      };

      const eventTypes = [
        'opportunity_detected',
        'position_opened',
        'position_closed',
        'risk_suspended',
        'heartbeat',
      ];

      for (const type of eventTypes) {
        es.addEventListener(type, (e: MessageEvent) => {
          if (cancelled) return;
          try {
            const data = JSON.parse(e.data);
            setLastEvent({ type, data });
          } catch {
            setLastEvent({ type, data: e.data });
          }
        });
      }
    }

    connect();

    return () => {
      cancelled = true;
      esRef.current?.close();
      esRef.current = null;
    };
  }, [url, token]);

  return { lastEvent, isConnected };
}
