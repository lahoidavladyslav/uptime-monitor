import { useMutation, useQueryClient } from "@tanstack/react-query";

import { client, unwrap } from "@/api/client";
import { ApiError } from "@/api/errors";
import { ME_KEY } from "@/app/queryClient";

export function useLogout() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => unwrap(client.POST("/api/v1/auth/logout")),
    onSettled: (_data, error) => {
      // Cleared on success, or when the session was already gone (401). Not on a network failure:
      // the server session is still alive then.
      if (!error || (error instanceof ApiError && error.status === 401)) {
        qc.removeQueries({ predicate: (q) => q.queryKey[0] !== ME_KEY[0] });
        qc.setQueryData(ME_KEY, null);
      }
    },
  });
}
