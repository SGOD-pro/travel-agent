"use client";

import { useState, useEffect, useRef, useCallback } from "react";
import type {
  RouteMapGeoJsonResponse,
  RouteVersionItem,
  RouteVersionHistoryResponse,
  OptimizationJobResponse,
} from "@/types/routing";

const STORAGE_ACTIVE_JOB_KEY = "swena_active_route_job";
const STORAGE_ACTIVE_ROUTE_KEY = "swena_active_route_id";

export interface ReplanSubmissionPayload {
  current_location: {
    latitude: number;
    longitude: number;
  };
  completed_stop_ids: string[];
  replan_at?: string;
}

export interface UseRouteOptimizationOptions {
  apiBase?: string;
  autoRecover?: boolean;
  onJobSucceeded?: (routeId: string) => void;
  onJobFailed?: (error: string) => void;
}

export function useRouteOptimization(options: UseRouteOptimizationOptions = {}) {
  const apiBase =
    options.apiBase ||
    process.env.NEXT_PUBLIC_API_BASE_URL ||
    "http://localhost:8000";

  const optionsRef = useRef(options);
  optionsRef.current = options;
  const autoRecover = options.autoRecover ?? true;

  const [jobId, setJobId] = useState<string | null>(() => {
    if (typeof window !== "undefined") {
      return new URLSearchParams(window.location.search).get("job_id");
    }
    return null;
  });
  const [routeId, setRouteId] = useState<string | null>(() => {
    if (typeof window !== "undefined") {
      return new URLSearchParams(window.location.search).get("route_id");
    }
    return null;
  });
  const [routeMap, setRouteMap] = useState<RouteMapGeoJsonResponse | null>(null);
  const [versions, setVersions] = useState<RouteVersionItem[]>([]);
  const [jobStatus, setJobStatus] = useState<
    "IDLE" | "PENDING" | "RUNNING" | "SUCCEEDED" | "FAILED" | "CANCELLED"
  >(() => {
    if (typeof window !== "undefined" && new URLSearchParams(window.location.search).get("job_id")) {
      return "RUNNING";
    }
    return "IDLE";
  });
  const [error, setError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [isPolling, setIsPolling] = useState<boolean>(() => {
    if (typeof window !== "undefined") {
      return !!new URLSearchParams(window.location.search).get("job_id");
    }
    return false;
  });
  const [isReplanning, setIsReplanning] = useState(false);

  const pollTimeoutRef = useRef<NodeJS.Timeout | null>(null);
  const pollIntervalRef = useRef<number>(2000);
  const consecutiveErrorsRef = useRef<number>(0);
  const hasRecoveredRef = useRef<boolean>(false);

  // Fetch GeoJSON Map representation for a given route_id
  const fetchRouteMap = useCallback(
    async (id: string): Promise<RouteMapGeoJsonResponse | null> => {
      try {
        const res = await fetch(`${apiBase}/api/v1/optimization/routes/${id}/map`, {
          headers: {
            "Content-Type": "application/json",
          },
        });
        if (!res.ok) {
          throw new Error(`Failed to fetch route map: HTTP ${res.status}`);
        }
        const data: RouteMapGeoJsonResponse = await res.json();
        setRouteMap(data);
        return data;
      } catch (err: any) {
        console.error("Error fetching route map GeoJSON:", err);
        setError(err.message || "Failed to load route map");
        return null;
      }
    },
    [apiBase]
  );

  // Fetch complete version history tree for a given route_id
  const fetchVersionHistory = useCallback(
    async (id: string): Promise<RouteVersionItem[]> => {
      try {
        const res = await fetch(
          `${apiBase}/api/v1/optimization/routes/${id}/versions`,
          {
            headers: {
              "Content-Type": "application/json",
            },
          }
        );
        if (!res.ok) {
          throw new Error(`Failed to fetch version history: HTTP ${res.status}`);
        }
        const data: RouteVersionHistoryResponse = await res.json();
        setVersions(data.versions || []);
        return data.versions || [];
      } catch (err: any) {
        console.error("Error fetching version history:", err);
        return [];
      }
    },
    [apiBase]
  );

  // Stop polling and clear timer
  const stopPolling = useCallback(() => {
    if (pollTimeoutRef.current) {
      clearTimeout(pollTimeoutRef.current);
      pollTimeoutRef.current = null;
    }
    setIsPolling(false);
  }, []);

  // Poll job status with exponential backoff (1s, 2s, 3s, max 5s)
  const pollJobStatus = useCallback(
    async (activeJobId: string) => {
      try {
        const res = await fetch(`${apiBase}/api/v1/jobs/${activeJobId}`, {
          headers: {
            "Content-Type": "application/json",
          },
        });

        if (!res.ok) {
          throw new Error(`Job status polling failed: HTTP ${res.status}`);
        }

        const job: OptimizationJobResponse = await res.json();
        setJobStatus(job.status);
        consecutiveErrorsRef.current = 0;

        if (job.status === "SUCCEEDED") {
          stopPolling();
          try {
            sessionStorage.removeItem(STORAGE_ACTIVE_JOB_KEY);
          } catch {
            // Ignore sessionStorage unavailability
          }

          let resolvedRouteId = job.result_ref?.route_id;

          // If not in result_ref, discover by execution ID directly
          if (!resolvedRouteId) {
            const discRes = await fetch(
              `${apiBase}/api/v1/optimization/routes/by-job/${activeJobId}`
            );
            if (discRes.ok) {
              const discRoute = await discRes.json();
              resolvedRouteId = discRoute.id;
            }
          }

          if (resolvedRouteId) {
            setRouteId(resolvedRouteId);
            try {
              sessionStorage.setItem(STORAGE_ACTIVE_ROUTE_KEY, resolvedRouteId);
            } catch {
              // Ignore
            }

            // Update URL without full refresh for clean refresh recovery
            if (typeof window !== "undefined" && window.history) {
              const url = new URL(window.location.href);
              url.searchParams.delete("job_id");
              url.searchParams.set("route_id", resolvedRouteId);
              window.history.replaceState({}, "", url.toString());
            }

            await fetchRouteMap(resolvedRouteId);
            await fetchVersionHistory(resolvedRouteId);
            optionsRef.current.onJobSucceeded?.(resolvedRouteId);
          }
          return;
        }

        if (job.status === "FAILED" || job.status === "CANCELLED") {
          stopPolling();
          try {
            sessionStorage.removeItem(STORAGE_ACTIVE_JOB_KEY);
          } catch {
            // Ignore
          }
          const errorMsg =
            job.error?.message ||
            "Route optimization job failed during solver execution.";
          setError(errorMsg);
          optionsRef.current.onJobFailed?.(errorMsg);
          return;
        }

        // Schedule next poll with exponential backoff capped at 5s
        pollIntervalRef.current = Math.min(
          pollIntervalRef.current + 1000,
          5000
        );
        pollTimeoutRef.current = setTimeout(() => {
          pollJobStatus(activeJobId);
        }, pollIntervalRef.current);
      } catch (err: any) {
        consecutiveErrorsRef.current += 1;
        console.error(
          `Polling error encountered (failure ${consecutiveErrorsRef.current}):`,
          err
        );

        // Terminate after 5 consecutive failures and surface an actionable error
        if (consecutiveErrorsRef.current >= 5) {
          stopPolling();
          const errorMsg =
            "Network connection lost during route optimization polling. Please verify network access and retry.";
          setError(errorMsg);
          optionsRef.current.onJobFailed?.(errorMsg);
          return;
        }

        pollIntervalRef.current = Math.min(
          pollIntervalRef.current + 1000,
          5000
        );
        pollTimeoutRef.current = setTimeout(() => {
          pollJobStatus(activeJobId);
        }, pollIntervalRef.current);
      }
    },
    [apiBase, stopPolling, fetchRouteMap, fetchVersionHistory]
  );

  // Start polling a specific job
  const startPolling = useCallback(
    (activeJobId: string) => {
      stopPolling();
      consecutiveErrorsRef.current = 0;
      setJobId(activeJobId);
      setIsPolling(true);
      setError(null);
      setJobStatus("RUNNING");
      pollIntervalRef.current = 2000;

      try {
        sessionStorage.setItem(STORAGE_ACTIVE_JOB_KEY, activeJobId);
      } catch {
        // Ignore
      }

      if (typeof window !== "undefined" && window.history) {
        const url = new URL(window.location.href);
        url.searchParams.set("job_id", activeJobId);
        window.history.replaceState({}, "", url.toString());
      }

      pollJobStatus(activeJobId);
    },
    [stopPolling, pollJobStatus]
  );

  // Submit initial route optimization request
  const submitRouteOptimization = useCallback(
    async (payload: any): Promise<string | null> => {
      setIsSubmitting(true);
      setError(null);

      try {
        const idempotencyKey =
          typeof crypto !== "undefined" && crypto.randomUUID
            ? crypto.randomUUID()
            : `route-${Date.now()}`;

        const res = await fetch(`${apiBase}/api/v1/optimization/routes`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "Idempotency-Key": idempotencyKey,
          },
          body: JSON.stringify(payload),
        });

        if (!res.ok) {
          const errData = await res.json().catch(() => ({}));
          throw new Error(
            errData.detail || `Route submission failed: HTTP ${res.status}`
          );
        }

        const data = await res.json();
        const newJobId = data.job_id;
        startPolling(newJobId);
        return newJobId;
      } catch (err: any) {
        console.error("Route submission exception:", err);
        setError(err.message || "Failed to submit route optimization request.");
        setJobStatus("FAILED");
        return null;
      } finally {
        setIsSubmitting(false);
      }
    },
    [apiBase, startPolling]
  );

  // Submit mid-trip replanning request
  const submitReplan = useCallback(
    async (payload: ReplanSubmissionPayload): Promise<string | null> => {
      if (!routeId) {
        setError("Cannot replan: no active route_id available.");
        return null;
      }

      setIsReplanning(true);
      setError(null);

      try {
        const expectedPrevVersion =
          routeMap?.properties?.route_version ?? routeMap?.route_version;

        const body: Record<string, any> = {
          current_location: payload.current_location,
          completed_stop_ids: payload.completed_stop_ids,
          replan_at: payload.replan_at || new Date().toISOString(),
        };

        if (expectedPrevVersion !== undefined) {
          body.expected_previous_version = expectedPrevVersion;
        }

        const res = await fetch(
          `${apiBase}/api/v1/optimization/routes/${routeId}/replans`,
          {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
            },
            body: JSON.stringify(body),
          }
        );

        if (res.status === 409) {
          throw new Error(
            "Replan conflict: the route has already been updated to a newer version. Please refresh."
          );
        }

        if (!res.ok) {
          const errData = await res.json().catch(() => ({}));
          throw new Error(
            errData.detail || `Replan submission failed: HTTP ${res.status}`
          );
        }

        const data = await res.json();
        const newJobId = data.job_id;
        startPolling(newJobId);
        return newJobId;
      } catch (err: any) {
        console.error("Replan exception:", err);
        setError(err.message || "Failed to submit replan request.");
        return null;
      } finally {
        setIsReplanning(false);
      }
    },
    [apiBase, routeId, routeMap, startPolling]
  );

  // Select and view an older or alternative version from the lineage tree
  const selectVersion = useCallback(
    async (versionRouteId: string) => {
      setRouteId(versionRouteId);
      if (typeof window !== "undefined" && window.history) {
        const url = new URL(window.location.href);
        url.searchParams.set("route_id", versionRouteId);
        window.history.replaceState({}, "", url.toString());
      }
      await fetchRouteMap(versionRouteId);
    },
    [fetchRouteMap]
  );

  // Refresh recovery on mount
  useEffect(() => {
    if (autoRecover === false || hasRecoveredRef.current) return;
    hasRecoveredRef.current = true;

    if (typeof window === "undefined") return;

    const searchParams = new URLSearchParams(window.location.search);
    const urlJobId = searchParams.get("job_id");
    const urlRouteId = searchParams.get("route_id");

    let savedJobId: string | null = null;
    let savedRouteId: string | null = null;
    try {
      savedJobId = sessionStorage.getItem(STORAGE_ACTIVE_JOB_KEY);
      savedRouteId = sessionStorage.getItem(STORAGE_ACTIVE_ROUTE_KEY);
    } catch {
      // Ignore
    }

    const effectiveJobId = urlJobId || savedJobId;
    const effectiveRouteId = urlRouteId || savedRouteId;

    if (effectiveJobId) {
      // Resume active job polling immediately upon page refresh
      startPolling(effectiveJobId);
    } else if (effectiveRouteId) {
      // Load already computed route
      setRouteId(effectiveRouteId);
      fetchRouteMap(effectiveRouteId);
      fetchVersionHistory(effectiveRouteId);
    }

    return () => {
      stopPolling();
    };
  }, [autoRecover, startPolling, stopPolling, fetchRouteMap, fetchVersionHistory]);

  return {
    jobId,
    routeId,
    routeMap,
    versions,
    jobStatus,
    error,
    isSubmitting,
    isPolling,
    isReplanning,
    submitRouteOptimization,
    submitReplan,
    selectVersion,
    startPolling,
    stopPolling,
    refetchMap: () => (routeId ? fetchRouteMap(routeId) : Promise.resolve(null)),
    refetchVersions: () =>
      routeId ? fetchVersionHistory(routeId) : Promise.resolve([]),
  };
}
