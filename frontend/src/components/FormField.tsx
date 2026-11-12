import { forwardRef, useId, type InputHTMLAttributes } from "react";

import { Input } from "./ui/input";
import { Label } from "./ui/label";

interface Props extends InputHTMLAttributes<HTMLInputElement> {
  label: string;
  error?: string;
}

export const FormField = forwardRef<HTMLInputElement, Props>(({ label, error, id, ...props }, ref) => {
  const auto = useId();
  const fieldId = id ?? auto;
  return (
    <div className="flex flex-col gap-1.5">
      <Label htmlFor={fieldId}>{label}</Label>
      <Input ref={ref} id={fieldId} aria-invalid={!!error} aria-describedby={error ? `${fieldId}-error` : undefined} {...props} />
      {error && (
        <p id={`${fieldId}-error`} className="text-sm text-status-down">
          {error}
        </p>
      )}
    </div>
  );
});
FormField.displayName = "FormField";

export function FormAlert({ message }: { message?: string }) {
  if (!message) return null;
  return (
    <div role="alert" className="rounded-lg border border-status-down/40 bg-status-down/10 px-3 py-2 text-sm text-status-down">
      {message}
    </div>
  );
}
