import { zodResolver } from "@hookform/resolvers/zod";
import { useForm } from "react-hook-form";
import { Link } from "react-router-dom";
import { z } from "zod";

import { FormAlert, FormField } from "@/components/FormField";
import { Button } from "@/components/ui/button";
import { applyApiErrors } from "@/lib/forms";
import { t } from "@/lib/strings";

import { AuthLayout } from "./AuthLayout";
import { usePasswordReset } from "./useAuthMutations";

const schema = z.object({ email: z.string().trim().min(1, t.form.required) });
type Values = z.infer<typeof schema>;

export function ForgotPasswordPage() {
  const reset = usePasswordReset();
  const { register, handleSubmit, setError, formState } = useForm<Values>({ resolver: zodResolver(schema) });

  const submit = handleSubmit((values) => {
    if (reset.isPending) return;
    reset.mutate(values.email, { onError: (e) => applyApiErrors(e, setError, ["email"]) });
  });

  return (
    <AuthLayout title={t.auth.forgotTitle}>
      {reset.isSuccess ? (
        // Same text for known and unknown addresses: the page must not reveal which accounts exist.
        <p role="status" className="text-sm">
          {t.auth.forgotDone}
        </p>
      ) : (
        <form onSubmit={submit} noValidate className="flex flex-col gap-4">
          <FormAlert message={formState.errors.root?.message} />
          <FormField label={t.auth.email} type="email" autoComplete="email" error={formState.errors.email?.message} {...register("email")} />
          <Button type="submit" disabled={reset.isPending}>
            {t.auth.forgotSend}
          </Button>
        </form>
      )}
      <p className="mt-4 text-sm">
        <Link to="/login" className="text-accent underline">
          {t.auth.loginLink}
        </Link>
      </p>
    </AuthLayout>
  );
}
