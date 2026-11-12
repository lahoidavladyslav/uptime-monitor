import type { FieldPath, FieldValues, UseFormSetError } from "react-hook-form";

import { ApiError } from "@/api/errors";
import { t } from "@/lib/strings";

/** Puts an API error on the form: field errors under their inputs, everything else in `root`. */
export function applyApiErrors<T extends FieldValues>(error: unknown, setError: UseFormSetError<T>, fields: readonly string[]) {
  if (!(error instanceof ApiError)) {
    setError("root", { message: t.errors.generic });
    return;
  }
  let placed = false;
  for (const [name, messages] of Object.entries(error.fieldErrors)) {
    const message = messages[0];
    if (!message) continue;
    if (fields.includes(name)) {
      setError(name as FieldPath<T>, { message });
      placed = true;
    } else {
      setError("root", { message });
      placed = true;
    }
  }
  if (!placed) setError("root", { message: error.message });
}
