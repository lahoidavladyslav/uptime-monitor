import { http, HttpResponse } from "msw";

import { t } from "@/lib/strings";
import { server } from "@/test/server";

import { createApiClient, unwrap } from "./client";
import { ApiError } from "./errors";

const client = createApiClient(window.location.origin);
const LOGIN = { body: { username: "alice", password: "pw" } };

describe("api client", () => {
  it("sends X-CSRFToken on POST using the cookie value at request time", async () => {
    const seen: (string | null)[] = [];
    server.use(
      http.post("*/api/v1/auth/login", ({ request }) => {
        seen.push(request.headers.get("X-CSRFToken"));
        return HttpResponse.json({ status: "ok" });
      }),
    );
    document.cookie = "csrftoken=t1";
    await unwrap(client.POST("/api/v1/auth/login", LOGIN));
    document.cookie = "csrftoken=t2"; // rotated after login
    await unwrap(client.POST("/api/v1/auth/login", LOGIN));
    expect(seen).toEqual(["t1", "t2"]);
  });

  it("does not send the CSRF header on GET", async () => {
    let header: string | null = "unset";
    server.use(
      http.get("*/api/v1/auth/me", ({ request }) => {
        header = request.headers.get("X-CSRFToken");
        return HttpResponse.json({ id: 1, username: "a", email: "a@a.a", has_2fa: false });
      }),
    );
    document.cookie = "csrftoken=t1";
    const me = await unwrap(client.GET("/api/v1/auth/me"));
    expect(header).toBeNull();
    expect(me.username).toBe("a");
  });

  it("turns a 422 into ApiError with per-field messages from the API", async () => {
    server.use(
      http.post("*/api/v1/auth/login", () =>
        HttpResponse.json({ message: "Перевірте введені дані.", errors: { username: ["Обов'язкове"] } }, { status: 422 }),
      ),
    );
    const err = await unwrap(client.POST("/api/v1/auth/login", LOGIN)).catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(422);
    expect(err.message).toBe("Перевірте введені дані.");
    expect(err.fieldErrors).toEqual({ username: ["Обов'язкове"] });
  });

  it("keeps the API's own message for a JSON 401", async () => {
    server.use(http.get("*/api/v1/auth/me", () => HttpResponse.json({ message: "Потрібен вхід.", errors: {} }, { status: 401 })));
    const err = await unwrap(client.GET("/api/v1/auth/me")).catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect([err.status, err.message]).toEqual([401, "Потрібен вхід."]);
  });

  it("survives an HTML 502 from a proxy (no JSON parse crash)", async () => {
    server.use(
      http.get("*/api/v1/auth/me", () => new HttpResponse("<html><body>Bad Gateway</body></html>", { status: 502, headers: { "Content-Type": "text/html" } })),
    );
    const err = await unwrap(client.GET("/api/v1/auth/me")).catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect([err.status, err.message]).toEqual([502, t.errors.server]);
    expect(err.fieldErrors).toEqual({});
  });

  it("survives an empty 500 body", async () => {
    server.use(http.get("*/api/v1/auth/me", () => new HttpResponse(null, { status: 500 })));
    const err = await unwrap(client.GET("/api/v1/auth/me")).catch((e) => e);
    expect([err.status, err.message]).toEqual([500, t.errors.server]);
  });

  it("maps a network failure to ApiError(0) with the network message", async () => {
    server.use(http.get("*/api/v1/auth/me", () => HttpResponse.error()));
    const err = await unwrap(client.GET("/api/v1/auth/me")).catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect([err.status, err.message]).toEqual([0, t.errors.network]);
  });
});

describe("api client without a CSRF cookie", () => {
  it("fetches the cookie first, then sends the token (cookie cleared mid-session)", async () => {
    const order: string[] = [];
    server.use(
      http.get("*/api/v1/auth/csrf", () => {
        order.push("csrf");
        document.cookie = "csrftoken=fresh-token";
        return HttpResponse.json({ status: "ok" });
      }),
      http.post("*/api/v1/auth/login", ({ request }) => {
        order.push(`login:${request.headers.get("X-CSRFToken")}`);
        return HttpResponse.json({ status: "ok" });
      }),
    );
    expect(document.cookie).not.toContain("csrftoken"); // the other tab cleared it
    await unwrap(client.POST("/api/v1/auth/login", LOGIN));
    expect(order).toEqual(["csrf", "login:fresh-token"]);
  });

  it("does not fetch it when the cookie exists, nor for GET requests", async () => {
    let csrfCalls = 0;
    server.use(
      http.get("*/api/v1/auth/csrf", () => ((csrfCalls += 1), HttpResponse.json({ status: "ok" }))),
      http.get("*/api/v1/auth/me", () => HttpResponse.json({ id: 1, username: "a", email: "a@a.a", has_2fa: false })),
    );
    await unwrap(client.GET("/api/v1/auth/me")); // GET without cookie: no token needed
    document.cookie = "csrftoken=have-it";
    server.use(http.post("*/api/v1/auth/login", () => HttpResponse.json({ status: "ok" })));
    await unwrap(client.POST("/api/v1/auth/login", LOGIN));
    expect(csrfCalls).toBe(0);
  });
});
