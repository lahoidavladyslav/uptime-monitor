/** A post-login destination is only ever a same-origin path ("/..."), never a URL or protocol-relative. */
export function safeNext(raw: string | null | undefined): string | null {
  if (!raw || !raw.startsWith("/") || raw.startsWith("//") || raw.includes("\\")) return null;
  try {
    const base = "http://safe.invalid";
    const url = new URL(raw, base);
    return url.origin === base ? url.pathname + url.search + url.hash : null;
  } catch {
    return null;
  }
}
