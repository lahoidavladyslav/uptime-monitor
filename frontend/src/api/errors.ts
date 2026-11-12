import { t } from "@/lib/strings";

export type FieldErrors = Record<string, string[]>;

export class ApiError extends Error {
  readonly status: number;
  readonly fieldErrors: FieldErrors;

  constructor(status: number, message: string, fieldErrors: FieldErrors = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.fieldErrors = fieldErrors;
  }
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null;
}

/** Turns whatever the server sent (our JSON error, an HTML proxy page, nothing) into an ApiError. */
export function toApiError(status: number, body: unknown): ApiError {
  if (isRecord(body) && typeof body.message === "string" && body.message) {
    const fieldErrors = isRecord(body.errors) ? (body.errors as FieldErrors) : {};
    return new ApiError(status, body.message, fieldErrors);
  }
  return new ApiError(status, status >= 500 ? t.errors.server : t.errors.generic);
}
