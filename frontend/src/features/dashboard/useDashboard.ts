import { useQuery } from "@tanstack/react-query";

import { client, unwrap } from "@/api/client";

export const DASHBOARD_POLL_MS = 10_000;

export function useDashboard() {
  return useQuery({
    queryKey: ["dashboard"],
    queryFn: () => unwrap(client.GET("/api/v1/dashboard")),
    refetchInterval: DASHBOARD_POLL_MS,
    refetchIntervalInBackground: false, // a hidden tab does not poll
  });
}
