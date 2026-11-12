import { safeNext } from "./nav";

describe("safeNext", () => {
  it.each([
    ["/", "/"],
    ["/monitors/5", "/monitors/5"],
    ["/monitors/5?tab=checks#top", "/monitors/5?tab=checks#top"],
  ])("accepts same-origin path %s", (raw, expected) => {
    expect(safeNext(raw)).toBe(expected);
  });

  it.each([null, undefined, "", "https://evil.example/", "//evil.example", "/\\evil.example", "javascript:alert(1)", "evil", "http://x"])(
    "rejects %s",
    (raw) => {
      expect(safeNext(raw)).toBeNull();
    },
  );
});
