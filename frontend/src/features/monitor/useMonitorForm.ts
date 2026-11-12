import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { client, unwrap } from "@/api/client";
import type { components } from "@/api/schema";

export type MonitorInput = components["schemas"]["MonitorIn"];

export function useMonitorConfig(id: number | null) {
  return useQuery({
    queryKey: ["monitor", id, "config"],
    enabled: id !== null,
    queryFn: () => unwrap(client.GET("/api/v1/monitors/{monitor_id}/config", { params: { path: { monitor_id: id! } } })),
    retry: (count, error) => !(error instanceof Error && "status" in error && error.status === 404) && count < 2,
  });
}

/** Creates (id = null) or replaces a monitor; the server validates with the same form as the old pages. */
export function useSaveMonitor(id: number | null) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: MonitorInput) =>
      id === null
        ? unwrap(client.POST("/api/v1/monitors", { body }))
        : unwrap(client.PUT("/api/v1/monitors/{monitor_id}", { params: { path: { monitor_id: id } }, body })),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["dashboard"] });
      if (id !== null) void qc.invalidateQueries({ queryKey: ["monitor", id] });
    },
  });
}

export function useDeleteMonitor(id: number) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => unwrap(client.DELETE("/api/v1/monitors/{monitor_id}", { params: { path: { monitor_id: id } } })),
    onSuccess: () => {
      qc.removeQueries({ queryKey: ["monitor", id] });
      void qc.invalidateQueries({ queryKey: ["dashboard"] });
    },
  });
}
