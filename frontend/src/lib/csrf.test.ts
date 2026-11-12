import { http, HttpResponse } from "msw";

import { server } from "@/test/server";

import { ensureCsrf, getCsrfToken } from "./csrf";

describe("getCsrfToken", () => {
  it("returns null when there is no csrftoken cookie", () => {
    expect(getCsrfToken()).toBeNull();
  });

  it("reads the cookie value", () => {
    document.cookie = "csrftoken=abc123";
    expect(getCsrfToken()).toBe("abc123");
  });

  it("reads the CURRENT value on every call (Django rotates the token on login)", () => {
    document.cookie = "csrftoken=before-login";
    expect(getCsrfToken()).toBe("before-login");
    document.cookie = "csrftoken=after-login";
    expect(getCsrfToken()).toBe("after-login");
  });

  it("ignores similarly named cookies", () => {
    document.cookie = "notcsrftoken=nope";
    expect(getCsrfToken()).toBeNull();
  });
});

describe("ensureCsrf", () => {
  function counting() {
    let calls = 0;
    server.use(
      http.get("*/api/v1/auth/csrf", () => {
        calls += 1;
        document.cookie = "csrftoken=fresh"; // what Set-Cookie would do in a real browser
        return HttpResponse.json({ status: "ok" });
      }),
    );
    return () => calls;
  }

  it("fetches the cookie once when it is missing, then not again", async () => {
    const calls = counting();
    await ensureCsrf();
    await ensureCsrf();
    expect(calls()).toBe(1);
    expect(getCsrfToken()).toBe("fresh");
  });

  it("does not hit the network when the cookie already exists", async () => {
    const calls = counting();
    document.cookie = "csrftoken=present";
    await ensureCsrf();
    expect(calls()).toBe(0);
  });
});
