import { MutationCache, QueryCache, QueryClient } from "@tanstack/react-query";

import { ApiError } from "@/api/errors";

export const ME_KEY = ["me"] as const;
const NEVER_RETRY = [401, 403, 404, 422];

export function shouldRetry(failureCount: number, error: unknown): boolean {
  if (error instanceof ApiError && NEVER_RETRY.includes(error.status)) return false;
  return failureCount < 2;
}

export function createQueryClient(): QueryClient {
  // A 401 anywhere means the session is gone: forget the user once. The route guard then redirects
  // to /login, which unmounts the failing screen, so polling stops by itself (no retry storm).
  const onError = (error: unknown) => {
    if (error instanceof ApiError && error.status === 401) queryClient.setQueryData(ME_KEY, null);
  };
  const queryClient: QueryClient = new QueryClient({
    queryCache: new QueryCache({ onError }),
    mutationCache: new MutationCache({ onError }),
    defaultOptions: { queries: { retry: shouldRetry, refetchOnWindowFocus: false } },
  });
  return queryClient;
}
