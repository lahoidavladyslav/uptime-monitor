import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";

// Default handler: the API client fetches the CSRF cookie on demand. Tests may override it.
export const server = setupServer(http.get("*/api/v1/auth/csrf", () => HttpResponse.json({ status: "ok" })));
