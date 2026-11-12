import { zodResolver } from "@hookform/resolvers/zod";
import { useState } from "react";
import { useForm } from "react-hook-form";
import { z } from "zod";

import { ApiError } from "@/api/errors";
import { FormAlert, FormField } from "@/components/FormField";
import { Button } from "@/components/ui/button";
import { applyApiErrors } from "@/lib/forms";
import { t } from "@/lib/strings";

import { useVerify2fa } from "./useAuthMutations";

const schema = z.object({ code: z.string().trim().min(1, t.form.required) });
type Values = z.infer<typeof schema>;

export function TwoFactorForm({ onExpired }: { onExpired: (message: string) => void }) {
  const [recovery, setRecovery] = useState(false);
  const verify = useVerify2fa();
  const { register, handleSubmit, setError, formState } = useForm<Values>({ resolver: zodResolver(schema) });

  const submit = handleSubmit((values) => {
    if (verify.isPending) return;
    verify.mutate(values, {
      onError: (e) => {
        if (e instanceof ApiError && e.status === 401) onExpired(e.message || t.auth.sessionExpired);
        else applyApiErrors(e, setError, ["code"]);
      },
    });
  });

  return (
    <form onSubmit={submit} noValidate className="flex flex-col gap-4">
      <p className="text-sm text-muted-foreground">{recovery ? t.auth.recoveryHint : t.auth.twoFactorHint}</p>
      <FormAlert message={formState.errors.root?.message} />
      <FormField
        label={t.auth.code}
        autoFocus
        autoComplete="one-time-code"
        inputMode={recovery ? "text" : "numeric"}
        error={formState.errors.code?.message}
        {...register("code")}
      />
      <Button type="submit" disabled={verify.isPending}>
        {t.auth.verify}
      </Button>
      <Button type="button" variant="ghost" size="sm" onClick={() => setRecovery((r) => !r)}>
        {recovery ? t.auth.useAuthenticator : t.auth.useRecovery}
      </Button>
    </form>
  );
}
