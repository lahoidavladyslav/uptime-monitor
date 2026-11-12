import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { client, unwrap } from "@/api/client";

export type Period = "24h" | "7d";
export const MONITOR_POLL_MS = 10_000;

const key = (id: number, ...rest: unknown[]) => ["monitor", id, ...rest] as const;

export function useMonitor(id: number) {
  return useQuery({
    queryKey: key(id),
    queryFn: () => unwrap(client.GET("/api/v1/monitors/{monitor_id}", { params: { path: { monitor_id: id } } })),
    refetchInterval: MONITOR_POLL_MS,
    refetchIntervalInBackground: false,
    retry: (count, error) => !(error instanceof Error && "status" in error && error.status === 404) && count < 2,
  });
}

export function useStats(id: number, period: Period) {
  return useQuery({
    queryKey: key(id, "stats", period),
    queryFn: () => unwrap(client.GET("/api/v1/monitors/{monitor_id}/stats", { params: { path: { monitor_id: id }, query: { period } } })),
    placeholderData: keepPreviousData,
  });
}

export function useSeries(id: number, period: Period) {
  return useQuery({
    queryKey: key(id, "series", period),
    queryFn: () => unwrap(client.GET("/api/v1/monitors/{monitor_id}/series", { params: { path: { monitor_id: id }, query: { period } } })),
    placeholderData: keepPreviousData,
  });
}

export function useChecks(id: number, page: number, onlyFailed: boolean) {
  return useQuery({
    queryKey: key(id, "checks", page, onlyFailed),
    queryFn: () =>
      unwrap(
        client.GET("/api/v1/monitors/{monitor_id}/checks", {
          params: { path: { monitor_id: id }, query: { page, page_size: 20, only_failed: onlyFailed } },
        }),
      ),
    placeholderData: keepPreviousData,
  });
}

export function useIncidents(id: number) {
  return useQuery({
    queryKey: key(id, "incidents"),
    queryFn: () => unwrap(client.GET("/api/v1/monitors/{monitor_id}/incidents", { params: { path: { monitor_id: id } } })),
  });
}

export function useMaintenance(id: number) {
  return useQuery({
    queryKey: key(id, "maintenance"),
    queryFn: () => unwrap(client.GET("/api/v1/monitors/{monitor_id}/maintenance", { params: { path: { monitor_id: id } } })),
  });
}

export function useCheckNow(id: number) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => unwrap(client.POST("/api/v1/monitors/{monitor_id}/check", { params: { path: { monitor_id: id } } })),
    // the worker needs a moment: refresh now (status) and again shortly (the new check row)
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: key(id) });
      setTimeout(() => void qc.invalidateQueries({ queryKey: key(id) }), 3000);
    },
  });
}

export function useCreateWindow(id: number) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { starts_at: string; ends_at: string; reason: string }) =>
      // one-off, this monitor only; recurrence and "all monitors" are API features the form does not expose yet
      unwrap(
        client.POST("/api/v1/monitors/{monitor_id}/maintenance", {
          params: { path: { monitor_id: id } },
          body: { ...body, rrule: "", timezone: "UTC", all_monitors: false },
        }),
      ),
    onSuccess: () => void qc.invalidateQueries({ queryKey: key(id) }),
  });
}

export function useDeleteWindow(id: number) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (windowId: number) =>
      unwrap(client.DELETE("/api/v1/monitors/{monitor_id}/maintenance/{window_id}", { params: { path: { monitor_id: id, window_id: windowId } } })),
    onSuccess: () => void qc.invalidateQueries({ queryKey: key(id) }),
  });
}
