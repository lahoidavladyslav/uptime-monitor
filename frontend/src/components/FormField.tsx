import {
  forwardRef,
  useId,
  type InputHTMLAttributes,
  type SelectHTMLAttributes,
  type TextareaHTMLAttributes,
} from "react";

import { Input } from "./ui/input";
import { Label } from "./ui/label";

interface Props extends InputHTMLAttributes<HTMLInputElement> {
  label: string;
  error?: string;
  hint?: string;
}

export const FormField = forwardRef<HTMLInputElement, Props>(
  ({ label, error, hint, id, ...props }, ref) => {
    const auto = useId();
    const fieldId = id ?? auto;
    return (
      <div className="flex flex-col gap-1.5">
        <Label htmlFor={fieldId}>{label}</Label>
        <Input
          ref={ref}
          id={fieldId}
          aria-invalid={!!error}
          aria-describedby={
            error ? `${fieldId}-error` : hint ? `${fieldId}-hint` : undefined
          }
          {...props}
        />
        {hint && !error && (
          <p id={`${fieldId}-hint`} className="text-xs text-muted-foreground">
            {hint}
          </p>
        )}
        {error && (
          <p id={`${fieldId}-error`} className="text-sm text-status-down">
            {error}
          </p>
        )}
      </div>
    );
  },
);
FormField.displayName = "FormField";

export function FormAlert({ message }: { message?: string }) {
  if (!message) return null;
  return (
    <div
      role="alert"
      className="rounded-lg border border-status-down/40 bg-status-down/10 px-3 py-2 text-sm text-status-down"
    >
      {message}
    </div>
  );
}

interface FieldChrome {
  label: string;
  error?: string;
  hint?: string;
}

export const FormSelect = forwardRef<
  HTMLSelectElement,
  FieldChrome & SelectHTMLAttributes<HTMLSelectElement>
>(({ label, error, hint, id, children, ...props }, ref) => {
  const auto = useId();
  const fieldId = id ?? auto;
  return (
    <div className="flex flex-col gap-1.5">
      <Label htmlFor={fieldId}>{label}</Label>
      <select
        ref={ref}
        id={fieldId}
        aria-invalid={!!error}
        aria-describedby={
          error ? `${fieldId}-error` : hint ? `${fieldId}-hint` : undefined
        }
        className="h-9 w-full rounded-lg border border-border bg-card px-3 text-sm aria-[invalid=true]:border-status-down"
        {...props}
      >
        {children}
      </select>
      {hint && !error && (
        <p id={`${fieldId}-hint`} className="text-xs text-muted-foreground">
          {hint}
        </p>
      )}
      {error && (
        <p id={`${fieldId}-error`} className="text-sm text-status-down">
          {error}
        </p>
      )}
    </div>
  );
});
FormSelect.displayName = "FormSelect";

export const FormTextarea = forwardRef<
  HTMLTextAreaElement,
  FieldChrome & TextareaHTMLAttributes<HTMLTextAreaElement>
>(({ label, error, hint, id, ...props }, ref) => {
  const auto = useId();
  const fieldId = id ?? auto;
  return (
    <div className="flex flex-col gap-1.5">
      <Label htmlFor={fieldId}>{label}</Label>
      <textarea
        ref={ref}
        id={fieldId}
        rows={3}
        aria-invalid={!!error}
        aria-describedby={
          error ? `${fieldId}-error` : hint ? `${fieldId}-hint` : undefined
        }
        className="w-full rounded-lg border border-border bg-card px-3 py-2 font-mono text-sm placeholder:text-muted-foreground disabled:opacity-50 aria-[invalid=true]:border-status-down"
        {...props}
      />
      {hint && !error && (
        <p id={`${fieldId}-hint`} className="text-xs text-muted-foreground">
          {hint}
        </p>
      )}
      {error && (
        <p id={`${fieldId}-error`} className="text-sm text-status-down">
          {error}
        </p>
      )}
    </div>
  );
});
FormTextarea.displayName = "FormTextarea";

export const FormCheckbox = forwardRef<
  HTMLInputElement,
  FieldChrome & InputHTMLAttributes<HTMLInputElement>
>(({ label, error, hint, id, ...props }, ref) => {
  const auto = useId();
  const fieldId = id ?? auto;
  return (
    <div className="flex flex-col gap-1">
      <label
        htmlFor={fieldId}
        className="flex cursor-pointer items-center gap-2 text-sm font-medium"
      >
        <input
          ref={ref}
          id={fieldId}
          type="checkbox"
          aria-invalid={!!error}
          className="size-4"
          {...props}
        />
        {label}
      </label>
      {hint && <p className="ml-6 text-xs text-muted-foreground">{hint}</p>}
      {error && <p className="ml-6 text-sm text-status-down">{error}</p>}
    </div>
  );
});
FormCheckbox.displayName = "FormCheckbox";
