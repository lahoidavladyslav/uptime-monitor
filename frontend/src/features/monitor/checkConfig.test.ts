import { buildConfig, CHECK_DEFAULTS, configErrorsByField, formatEquals, parseEquals, parseStatuses, readConfig, type CheckFields } from "./checkConfig";

const v = (over: Partial<CheckFields>): CheckFields => ({ ...CHECK_DEFAULTS, ...over });

describe("statuses", () => {
  it("parses codes and ranges and leaves garbage for the server to reject", () => {
    expect(parseStatuses("200, 300-399 ,")).toEqual([200, "300-399"]);
    expect(parseStatuses("abc")).toEqual(["abc"]);
    expect(parseStatuses("")).toEqual([]);
  });
});

describe("equals", () => {
  it.each([
    ["ok", "ok"],
    ["200", 200],
    ["true", true],
    ["null", null],
    ['"200"', "200"],
    ["1.5", 1.5],
  ])("%s -> %j", (raw, expected) => {
    expect(parseEquals(raw)).toBe(expected);
  });

  it("round-trips: what the user typed is what they see again after saving", () => {
    for (const value of ["ok", 200, true, null, "200", "true", "null", 1.5]) {
      expect(parseEquals(formatEquals(value))).toBe(value);
    }
  });
});

describe("buildConfig / readConfig", () => {
  it("builds the config of each type and only includes statuses when given", () => {
    expect(buildConfig(v({}))).toEqual({});
    expect(buildConfig(v({ cfg_statuses: "200,301" }))).toEqual({ expected_statuses: [200, 301] });
    expect(buildConfig(v({ check_type: "keyword", cfg_keyword: "Welcome", cfg_must_contain: false }))).toEqual({ keyword: "Welcome", must_contain: false });
    expect(buildConfig(v({ check_type: "json_path", cfg_path: " status.db ", cfg_equals: "ok" }))).toEqual({ path: "status.db", equals: "ok" });
    expect(buildConfig(v({ check_type: "ssl_expiry", cfg_min_days: "7", cfg_statuses: "200" }))).toEqual({ min_days_left: 7 }); // no HTTP status for SSL
  });

  it("reading what was built gives the same fields back", () => {
    for (const fields of [
      v({ cfg_statuses: "200,301" }),
      v({ check_type: "keyword", cfg_keyword: "x", cfg_must_contain: false }),
      v({ check_type: "json_path", cfg_path: "a.0.b", cfg_equals: "200" }),
      v({ check_type: "ssl_expiry", cfg_min_days: "30" }),
    ]) {
      const again = readConfig(fields.check_type, buildConfig(fields));
      expect(buildConfig(again)).toEqual(buildConfig(fields));
    }
  });

  it("an unknown type from the server falls back to a plain status check instead of crashing the form", () => {
    expect(readConfig("telnet", {}).check_type).toBe("http_status");
  });
});

describe("server errors", () => {
  it("are split per setting, and anything else lands on the type select", () => {
    expect(configErrorsByField(["keyword: is required, 1-200 characters", "expected_statuses: must be a list", "config: must be a JSON object"])).toEqual({
      cfg_keyword: "is required, 1-200 characters",
      cfg_statuses: "must be a list",
      check_type: "config: must be a JSON object",
    });
  });
});
