import { QueryClientProvider } from "@tanstack/react-query";
import { renderHook, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";

import { useMe } from "@/features/auth/useMe";
import { server } from "@/test/server";
import { mockAnonymous, mockAuthed, renderApp } from "@/test/utils";

import { createQueryClient } from "./queryClient";

describe("route guards", () => {
  it("anonymous visit to / lands on /login?next=%2F", async () => {
    mockAnonymous();
    const { router } = renderApp("/");
    await waitFor(() => expect(router.state.location.pathname).toBe("/login"));
    expect(router.state.location.search).toBe("?next=%2F");
  });

  it("anonymous visit to a deep link keeps it in next", async () => {
    mockAnonymous();
    const { router } = renderApp("/monitors/5?tab=checks");
    await waitFor(() => expect(router.state.location.pathname).toBe("/login"));
    expect(router.state.location.search).toBe("?next=%2Fmonitors%2F5%3Ftab%3Dchecks");
  });

  it("an authenticated user is sent away from /login to /", async () => {
    mockAuthed();
    const { router } = renderApp("/login");
    await waitFor(() => expect(router.state.location.pathname).toBe("/"));
    expect(await screen.findByText("alice")).toBeInTheDocument(); // inside the app shell
  });

  it("unknown routes render the not-found page", async () => {
    mockAuthed();
    renderApp("/definitely/not/here");
    expect(await screen.findByText("Сторінку не знайдено")).toBeInTheDocument();
  });

  it("logout ends the session and returns to /login", async () => {
    mockAuthed();
    let loggedOut = false;
    server.use(
      http.get("*/api/v1/auth/me", () =>
        loggedOut ? HttpResponse.json({ message: "Потрібен вхід.", errors: {} }, { status: 401 }) : HttpResponse.json({ id: 1, username: "alice", email: "a@a.a", has_2fa: false }),
      ),
      http.post("*/api/v1/auth/logout", () => {
        loggedOut = true;
        return HttpResponse.json({ status: "ok" });
      }),
    );
    const { router } = renderApp("/");
    await userEvent.click(await screen.findByRole("button", { name: "Вийти" }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/login"));
  });
});

describe("app start", () => {
  it("asks for the CSRF cookie exactly once", async () => {
    let calls = 0;
    mockAnonymous();
    server.use(
      http.get("*/api/v1/auth/csrf", () => {
        calls += 1;
        document.cookie = "csrftoken=abc";
        return HttpResponse.json({ status: "ok" });
      }),
    );
    const { router } = renderApp("/login");
    await waitFor(() => expect(router.state.location.pathname).toBe("/login"));
    await waitFor(() => expect(calls).toBe(1));
  });
});

describe("useMe", () => {
  it("maps a 401 to null instead of an error state", async () => {
    mockAnonymous();
    const qc = createQueryClient();
    const { result } = renderHook(() => useMe(), { wrapper: ({ children }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider> });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toBeNull();
    expect(result.current.isError).toBe(false);
  });

  it("returns the user for a 200", async () => {
    mockAuthed();
    const qc = createQueryClient();
    const { result } = renderHook(() => useMe(), { wrapper: ({ children }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider> });
    await waitFor(() => expect(result.current.data?.username).toBe("alice"));
  });
});
