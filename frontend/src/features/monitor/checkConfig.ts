/** Pure helpers between the form's flat fields and the API's `check_type` + `check_config`. */

export const CHECK_TYPES = [
  "http_status",
  "keyword",
  "json_path",
  "ssl_expiry",
] as const;
export type CheckType = (typeof CHECK_TYPES)[number];

export interface CheckFields {
  check_type: CheckType;
  cfg_statuses: string; // "200,204,300-399"
  cfg_keyword: string;
  cfg_must_contain: boolean;
  cfg_path: string;
  cfg_equals: string; // raw text: JSON if it parses (200, true, null, "ok"), otherwise a plain string
  cfg_min_days: string;
}

export const CHECK_DEFAULTS: CheckFields = {
  check_type: "http_status",
  cfg_statuses: "",
  cfg_keyword: "",
  cfg_must_contain: true,
  cfg_path: "",
  cfg_equals: "",
  cfg_min_days: "14",
};

/** "200, 300-399" -> [200, "300-399"]; garbage stays a string so the server can reject it with its own message. */
export function parseStatuses(text: string): (number | string)[] {
  return text
    .split(",")
    .map((p) => p.trim())
    .filter(Boolean)
    .map((p) => (/^\d+$/.test(p) ? Number(p) : p));
}

export function formatStatuses(list: unknown): string {
  return Array.isArray(list) ? list.join(",") : "";
}

/** `ok` -> "ok", `200` -> 200, `true` -> true, `null` -> null, `"200"` (quoted) -> the string "200". */
export function parseEquals(raw: string): string | number | boolean | null {
  const text = raw.trim();
  try {
    const value: unknown = JSON.parse(text);
    if (
      value === null ||
      ["string", "number", "boolean"].includes(typeof value)
    )
      return value as string | number | boolean | null;
  } catch {
    /* not JSON: a plain string */
  }
  return raw;
}

export function formatEquals(value: unknown): string {
  if (typeof value === "string")
    return /^(null|true|false|-?\d+(\.\d+)?|".*")$/.test(value.trim())
      ? JSON.stringify(value)
      : value;
  return JSON.stringify(value ?? null);
}

export function buildConfig(v: CheckFields): Record<string, unknown> {
  const statuses = parseStatuses(v.cfg_statuses);
  const withStatuses = (config: Record<string, unknown>) =>
    statuses.length ? { ...config, expected_statuses: statuses } : config;
  switch (v.check_type) {
    case "http_status":
      return withStatuses({});
    case "keyword":
      return withStatuses({
        keyword: v.cfg_keyword,
        must_contain: v.cfg_must_contain,
      });
    case "json_path":
      return withStatuses({
        path: v.cfg_path.trim(),
        equals: parseEquals(v.cfg_equals),
      });
    case "ssl_expiry":
      return { min_days_left: Number(v.cfg_min_days) };
  }
}

export function readConfig(
  type: string,
  config: Record<string, unknown>,
): CheckFields {
  const known = (CHECK_TYPES as readonly string[]).includes(type)
    ? (type as CheckType)
    : "http_status";
  return {
    ...CHECK_DEFAULTS,
    check_type: known,
    cfg_statuses: formatStatuses(config.expected_statuses),
    cfg_keyword: typeof config.keyword === "string" ? config.keyword : "",
    cfg_must_contain: config.must_contain !== false,
    cfg_path: typeof config.path === "string" ? config.path : "",
    cfg_equals: "equals" in config ? formatEquals(config.equals) : "",
    cfg_min_days:
      typeof config.min_days_left === "number"
        ? String(config.min_days_left)
        : CHECK_DEFAULTS.cfg_min_days,
  };
}

const SETTING_TO_FIELD: Record<string, keyof CheckFields> = {
  keyword: "cfg_keyword",
  must_contain: "cfg_must_contain",
  path: "cfg_path",
  equals: "cfg_equals",
  expected_statuses: "cfg_statuses",
  min_days_left: "cfg_min_days",
};

/** The server reports config problems as `check_config: ["keyword: is required", ...]`: split them per input. */
export function configErrorsByField(
  messages: string[],
): Partial<Record<keyof CheckFields, string>> {
  const out: Partial<Record<keyof CheckFields, string>> = {};
  for (const message of messages) {
    const [setting, ...rest] = message.split(": ");
    const field = SETTING_TO_FIELD[setting ?? ""];
    if (field && rest.length) out[field] ??= rest.join(": ");
    else out.check_type ??= message;
  }
  return out;
}
