"use client";
import { useEffect, useState, useCallback } from "react";

export interface JobEvent {
  event_type: string;
  job_id: string;
  timestamp: string;
  data: {
    phase?: string;
    status: string;
    message: string;
    progress?: number;
    [key: string]: unknown;
  };
}

const TERMINAL_EVENTS = ["job:completed", "job:failed"];

const EVENT_TYPES = [
  "job:started",
  "job:progress",
  "job:completed",
  "job:failed",
  "phase:started",
  "phase:completed",
  "phase:failed",
  "scorecard:completed",
  "ranking:completed",
  "heartbeat",
];

export function useJobStream(jobId: string | null) {
  const [events, setEvents] = useState<JobEvent[]>([]);
  const [isConnected, setIsConnected] = useState(false);
  const [isComplete, setIsComplete] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const connect = useCallback(() => {
    if (!jobId) return;

    const token =
      typeof window !== "undefined"
        ? localStorage.getItem("access_token")
        : null;
    if (!token) {
      setError("Authentication token missing");
      return;
    }

    const baseUrl =
      process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
    const eventSource = new EventSource(
      `${baseUrl}/api/v1/jobs/stream/${jobId}?token=${token}`
    );

    eventSource.onopen = () => {
      setIsConnected(true);
      setError(null);
    };

    eventSource.onerror = () => {
      setError("Connection to event stream failed");
      eventSource.close();
      setIsConnected(false);
    };

    EVENT_TYPES.forEach((type) => {
      eventSource.addEventListener(type, (event: MessageEvent) => {
        const payload: JobEvent = JSON.parse(event.data);
        setEvents((prev) => [...prev, payload]);
        if (TERMINAL_EVENTS.includes(type)) {
          setIsComplete(true);
          eventSource.close();
          setIsConnected(false);
        }
      });
    });

    return () => {
      eventSource.close();
      setIsConnected(false);
    };
  }, [jobId]);

  useEffect(() => {
    let cleanup: (() => void) | undefined;
    if (jobId) cleanup = connect() ?? undefined;
    return () => cleanup?.();
  }, [jobId, connect]);

  return { events, isConnected, isComplete, error };
}
