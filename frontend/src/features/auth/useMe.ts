import { useQuery } from "@tanstack/react-query";

import { ApiError } from "@/api/errors";
import { client, unwrap } from "@/api/client";
import type { components } from "@/api/schema";
import { ME_KEY } from "@/app/queryClient";

export type User = components["schemas"]["UserOut"];

/** The signed-in user, or `null` when anonymous (a 401 is an answer here, not an error). */
export function useMe() {
  return useQuery<User | null>({
    queryKey: ME_KEY,
    staleTime: 60_000,
    queryFn: async () => {
      try {
        return await unwrap(client.GET("/api/v1/auth/me"));
      } catch (e) {
        if (e instanceof ApiError && e.status === 401) return null;
        throw e;
      }
    },
  });
}
