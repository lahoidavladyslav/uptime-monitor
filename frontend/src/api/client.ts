import createClient from "openapi-fetch";

import { ensureCsrf, getCsrfToken } from "@/lib/csrf";
import { t } from "@/lib/strings";

import { ApiError, toApiError } from "./errors";
import type { paths } from "./schema";

const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS", "TRACE"]);

export function createApiClient(baseUrl: string = window.location.origin) {
  // `fetch` is looked up per call: openapi-fetch would otherwise freeze whatever global existed when the
  // client was created (e.g. before a test server or polyfill patched it).
  const client = createClient<paths>({
    baseUrl,
    credentials: "same-origin",
    fetch: (request) => globalThis.fetch(request),
  });
  client.use({
    async onRequest({ request }) {
      if (!SAFE_METHODS.has(request.method)) {
        let token = getCsrfToken(); // read now, not at startup: login rotates it
        if (!token) {
          await ensureCsrf(); // the cookie vanished mid-session (cleared elsewhere): get a fresh one
          token = getCsrfToken();
        }
        if (token) request.headers.set("X-CSRFToken", token);
      }
      return request;
    },
  });
  return client;
}

export const client = createApiClient();

type FetchResult<T> = { data?: T; error?: unknown; response: Response };

/** Returns the data of a successful call, throws ApiError for everything else. */
export async function unwrap<T>(call: Promise<FetchResult<T>>): Promise<T> {
  let result: FetchResult<T>;
  try {
    result = await call;
  } catch {
    throw new ApiError(0, t.errors.network);
  }
  if (!result.response.ok || result.error !== undefined) {
    throw toApiError(result.response.status, result.error);
  }
  return result.data as T;
}
