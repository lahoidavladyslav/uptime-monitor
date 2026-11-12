import { zodResolver } from "@hookform/resolvers/zod";
import { useState } from "react";
import { useForm } from "react-hook-form";
import { Link, useLocation, useSearchParams } from "react-router-dom";
import { z } from "zod";

import { FormAlert, FormField } from "@/components/FormField";
import { Button } from "@/components/ui/button";
import { applyApiErrors } from "@/lib/forms";
import { t } from "@/lib/strings";

import { AuthLayout } from "./AuthLayout";
import { TwoFactorForm } from "./TwoFactorForm";
import { useLogin } from "./useAuthMutations";

const schema = z.object({
  username: z.string().trim().min(1, t.form.required),
  password: z.string().min(1, t.form.required),
});
type Values = z.infer<typeof schema>;

export function LoginPage() {
  const [step, setStep] = useState<"password" | "2fa">("password");
  const location = useLocation();
  const [notice, setNotice] = useState<string | null>((location.state as { notice?: string } | null)?.notice ?? null);
  const [params] = useSearchParams();
  const login = useLogin();
  const { register, handleSubmit, setError, formState } = useForm<Values>({ resolver: zodResolver(schema) });

  const submit = handleSubmit((values) => {
    if (login.isPending) return;
    login.mutate(values, {
      onSuccess: (res) => {
        if (res.status === "2fa_required") {
          setNotice(null);
          setStep("2fa");
        }
      },
      onError: (e) => applyApiErrors(e, setError, ["username", "password"]),
    });
  });

  if (step === "2fa") {
    return (
      <AuthLayout title={t.auth.twoFactorTitle}>
        <TwoFactorForm
          onExpired={(message) => {
            setNotice(message);
            setStep("password");
          }}
        />
      </AuthLayout>
    );
  }

  const next = params.get("next");
  const withNext = (path: string) => (next ? `${path}?next=${encodeURIComponent(next)}` : path);
  return (
    <AuthLayout title={t.auth.loginTitle}>
      {notice && (
        <p role="status" className="mb-4 rounded-lg border border-border bg-muted px-3 py-2 text-sm">
          {notice}
        </p>
      )}
      <form onSubmit={submit} noValidate className="flex flex-col gap-4">
        <FormAlert message={formState.errors.root?.message} />
        <FormField label={t.auth.username} autoComplete="username" error={formState.errors.username?.message} {...register("username")} />
        <FormField
          label={t.auth.password}
          type="password"
          autoComplete="current-password"
          error={formState.errors.password?.message}
          {...register("password")}
        />
        <Button type="submit" disabled={login.isPending}>
          {t.auth.login}
        </Button>
      </form>
      <div className="mt-4 flex justify-between text-sm">
        <Link to="/forgot-password" className="text-accent underline">
          {t.auth.forgot}
        </Link>
        <span>
          {t.auth.noAccount}{" "}
          <Link to={withNext("/signup")} className="text-accent underline">
            {t.auth.signupLink}
          </Link>
        </span>
      </div>
    </AuthLayout>
  );
}
