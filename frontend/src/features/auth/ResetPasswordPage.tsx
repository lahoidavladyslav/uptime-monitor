import { zodResolver } from "@hookform/resolvers/zod";
import { useForm } from "react-hook-form";
import { Link, useNavigate, useParams } from "react-router-dom";
import { z } from "zod";

import { ApiError } from "@/api/errors";
import { FormAlert, FormField } from "@/components/FormField";
import { Button } from "@/components/ui/button";
import { applyApiErrors } from "@/lib/forms";
import { t } from "@/lib/strings";

import { AuthLayout } from "./AuthLayout";
import { usePasswordResetConfirm } from "./useAuthMutations";

const required = z.string().min(1, t.form.required);
const schema = z.object({ new_password1: required, new_password2: required });
type Values = z.infer<typeof schema>;

export function ResetPasswordPage() {
  const { uid = "", token = "" } = useParams();
  const navigate = useNavigate();
  const confirm = usePasswordResetConfirm();
  const { register, handleSubmit, setError, formState } = useForm<Values>({ resolver: zodResolver(schema) });
  const e = formState.errors;

  const submit = handleSubmit((values) => {
    if (confirm.isPending) return;
    confirm.mutate(
      { uid, token, ...values },
      {
        onSuccess: () => navigate("/login", { replace: true, state: { notice: t.auth.resetDone } }),
        onError: (err) => {
          if (!(err instanceof ApiError && err.status === 400)) applyApiErrors(err, setError, ["new_password1", "new_password2"]);
        },
      },
    );
  });

  if (confirm.error instanceof ApiError && confirm.error.status === 400) {
    return (
      <AuthLayout title={t.auth.resetTitle}>
        <p role="alert" className="mb-3 text-sm text-status-down">
          {confirm.error.message}
        </p>
        <Link to="/forgot-password" className="text-accent underline">
          {t.auth.requestNewLink}
        </Link>
      </AuthLayout>
    );
  }

  return (
    <AuthLayout title={t.auth.resetTitle}>
      <form onSubmit={submit} noValidate className="flex flex-col gap-4">
        <FormAlert message={e.root?.message} />
        <FormField label={t.auth.newPassword1} type="password" autoComplete="new-password" error={e.new_password1?.message} {...register("new_password1")} />
        <FormField label={t.auth.newPassword2} type="password" autoComplete="new-password" error={e.new_password2?.message} {...register("new_password2")} />
        <Button type="submit" disabled={confirm.isPending}>
          {t.auth.resetSave}
        </Button>
      </form>
    </AuthLayout>
  );
}
