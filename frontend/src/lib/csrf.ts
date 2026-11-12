const COOKIE = "csrftoken=";

/** Reads the CSRF cookie at call time. Never cache it: Django rotates the token on login. */
export function getCsrfToken(): string | null {
  const entry = document.cookie.split("; ").find((c) => c.startsWith(COOKIE));
  return entry ? decodeURIComponent(entry.slice(COOKIE.length)) : null;
}

let pending: Promise<void> | null = null;

/** Makes sure the CSRF cookie exists (one request, even if called concurrently). */
export function ensureCsrf(): Promise<void> {
  if (getCsrfToken()) return Promise.resolve();
  pending ??= fetch(`${window.location.origin}/api/v1/auth/csrf`, { credentials: "same-origin" })
    .then(() => undefined)
    .finally(() => {
      pending = null;
    });
  return pending;
}
