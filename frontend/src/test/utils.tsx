import type { QueryClient } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { RouterProvider } from "react-router-dom";

import { AppProviders } from "@/app/providers";
import { createQueryClient } from "@/app/queryClient";
import { createAppRouter } from "@/app/router";

import { server } from "./server";

export const USER = { id: 1, username: "alice", email: "alice@example.com", has_2fa: false };

export function mockAnonymous() {
  server.use(
    http.get("*/api/v1/auth/me", () => HttpResponse.json({ message: "Потрібен вхід.", errors: {} }, { status: 401 })),
    http.get("*/api/v1/auth/csrf", () => HttpResponse.json({ status: "ok" })),
  );
}

export function mockAuthed(user = USER) {
  server.use(
    http.get("*/api/v1/auth/me", () => HttpResponse.json(user)),
    http.get("*/api/v1/auth/csrf", () => HttpResponse.json({ status: "ok" })),
  );
}

export function stubMatchMedia() {
  window.matchMedia = (() => ({ matches: false, addEventListener() {}, removeEventListener() {} })) as never;
}

/** Renders the real app (providers + router) at `path`. */
export function renderApp(path = "/", queryClient: QueryClient = createQueryClient()) {
  stubMatchMedia();
  const router = createAppRouter([path]);
  const utils = render(
    <AppProviders queryClient={queryClient}>
      <RouterProvider router={router} future={{ v7_startTransition: true }} />
    </AppProviders>,
  );
  return { router, queryClient, ...utils };
}
