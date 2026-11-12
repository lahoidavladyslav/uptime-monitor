import { ApiError } from "@/api/errors";

import { createQueryClient, shouldRetry } from "./queryClient";

describe("shouldRetry", () => {
  it.each([401, 403, 404, 422])("never retries a %s", (status) => {
    expect(shouldRetry(0, new ApiError(status, "x"))).toBe(false);
  });

  it("retries transient failures at most twice", () => {
    expect(shouldRetry(0, new ApiError(500, "x"))).toBe(true);
    expect(shouldRetry(1, new ApiError(0, "x"))).toBe(true);
    expect(shouldRetry(2, new ApiError(500, "x"))).toBe(false);
  });
});

describe("global 401 handling", () => {
  it("any failing query with 401 clears the cached user once (=> guards redirect to login)", async () => {
    const qc = createQueryClient();
    qc.setQueryData(["me"], { id: 1 });
    await qc.fetchQuery({ queryKey: ["dashboard"], queryFn: () => Promise.reject(new ApiError(401, "Потрібен вхід.")), retry: false }).catch(() => {});
    expect(qc.getQueryData(["me"])).toBeNull();
  });

  it("other errors leave the user alone", async () => {
    const qc = createQueryClient();
    qc.setQueryData(["me"], { id: 1 });
    await qc.fetchQuery({ queryKey: ["x"], queryFn: () => Promise.reject(new ApiError(500, "boom")), retry: false }).catch(() => {});
    expect(qc.getQueryData(["me"])).toEqual({ id: 1 });
  });
});
