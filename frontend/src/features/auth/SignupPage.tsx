import { zodResolver } from "@hookform/resolvers/zod";
import { useForm } from "react-hook-form";
import { Link, useNavigate } from "react-router-dom";
import { z } from "zod";

import { FormAlert, FormField } from "@/components/FormField";
import { Button } from "@/components/ui/button";
import { applyApiErrors } from "@/lib/forms";
import { t } from "@/lib/strings";

import { AuthLayout } from "./AuthLayout";
import { useSignup } from "./useAuthMutations";

const required = z.string().trim().min(1, t.form.required);
const schema = z.object({ username: required, email: required, password1: required, password2: required });
type Values = z.infer<typeof schema>;
const FIELDS = ["username", "email", "password1", "password2"];

export function SignupPage() {
  const signup = useSignup();
  const navigate = useNavigate();
  const { register, handleSubmit, setError, formState } = useForm<Values>({ resolver: zodResolver(schema) });
  const e = formState.errors;

  const submit = handleSubmit((values) => {
    if (signup.isPending) return;
    signup.mutate(values, {
      onSuccess: () => navigate("/signup/sent", { state: { email: values.email } }),
      onError: (err) => applyApiErrors(err, setError, FIELDS),
    });
  });

  return (
    <AuthLayout title={t.auth.signupTitle}>
      <form onSubmit={submit} noValidate className="flex flex-col gap-4">
        <FormAlert message={e.root?.message} />
        <FormField label={t.auth.username} autoComplete="username" error={e.username?.message} {...register("username")} />
        <FormField label={t.auth.email} type="email" autoComplete="email" error={e.email?.message} {...register("email")} />
        <FormField label={t.auth.password1} type="password" autoComplete="new-password" error={e.password1?.message} {...register("password1")} />
        <FormField label={t.auth.password2} type="password" autoComplete="new-password" error={e.password2?.message} {...register("password2")} />
        <Button type="submit" disabled={signup.isPending}>
          {t.auth.signup}
        </Button>
      </form>
      <p className="mt-4 text-sm">
        {t.auth.haveAccount}{" "}
        <Link to="/login" className="text-accent underline">
          {t.auth.loginLink}
        </Link>
      </p>
    </AuthLayout>
  );
}
