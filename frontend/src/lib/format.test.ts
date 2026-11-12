import { formatDateTime, formatDuration, formatRelative, formatUptime } from "./format";

const NOW = new Date("2026-03-10T12:00:00Z");
const ago = (ms: number) => new Date(NOW.getTime() - ms).toISOString();

describe("formatRelative", () => {
  it.each([
    [3_000, "щойно"],
    [12_000, "12 с тому"],
    [5 * 60_000 + 3_000, "5 хв тому"],
    [3 * 3_600_000, "3 год тому"],
    [2 * 86_400_000, "2 дн тому"],
  ])("%d ms ago -> %s", (ms, expected) => {
    expect(formatRelative(ago(ms), NOW)).toBe(expected);
  });

  it("null, garbage and future timestamps (clock skew) never throw or show negatives", () => {
    expect(formatRelative(null, NOW)).toBe("—");
    expect(formatRelative("not a date", NOW)).toBe("—");
    expect(formatRelative(new Date(NOW.getTime() + 60_000).toISOString(), NOW)).toBe("щойно");
  });
});

describe("formatUptime", () => {
  it.each([
    [null, "—"],
    [100, "100%"],
    [0, "0%"],
    [99.9, "99.9%"],
    [99.94, "99.9%"],
    [99.99, "99.9%"], // never rounds up to a perfect 100% that was not perfect
    [87.5, "87.5%"],
  ])("%s -> %s", (v, expected) => {
    expect(formatUptime(v as number | null)).toBe(expected);
  });
});

describe("formatDuration / formatDateTime", () => {
  it.each([
    [0, "0 хв"],
    [42, "42 хв"],
    [60, "1 год"],
    [90, "1 год 30 хв"],
    [1440, "1 дн"],
    [1500, "1 дн 1 год"],
  ])("%i minutes -> %s", (minutes, expected) => {
    expect(formatDuration(minutes)).toBe(expected);
  });

  it("negative or fractional input does not produce nonsense", () => {
    expect(formatDuration(-5)).toBe("0 хв");
    expect(formatDuration(59.9)).toBe("59 хв");
  });

  it("garbage dates read as a dash", () => {
    expect(formatDateTime(null)).toBe("—");
    expect(formatDateTime("not-a-date")).toBe("—");
    expect(formatDateTime("2026-11-12T10:00:00Z")).toMatch(/2026|26/);
  });
});
