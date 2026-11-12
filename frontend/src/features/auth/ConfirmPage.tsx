import { useEffect, useRef } from "react";
import { Link, useParams } from "react-router-dom";

import { t } from "@/lib/strings";

import { AuthLayout } from "./AuthLayout";
import { useConfirm } from "./useAuthMutations";

export function ConfirmPage() {
  const { token = "" } = useParams();
  const confirm = useConfirm();
  const started = useRef(false); // a token is single-use: never fire twice (StrictMode re-runs effects)

  useEffect(() => {
    if (started.current) return;
    started.current = true;
    confirm.mutate(token);
  }, [confirm, token]);

  return (
    <AuthLayout title={t.auth.confirmTitle}>
      {confirm.isSuccess && (
        <>
          <p role="status" className="mb-3 text-sm">
            {t.auth.confirmed}
          </p>
          <Link to="/login" className="text-accent underline">
            {t.auth.loginLink}
          </Link>
        </>
      )}
      {confirm.isError && (
        <>
          <p role="alert" className="mb-3 text-sm text-status-down">
            {confirm.error.message}
          </p>
          <Link to="/resend" className="text-accent underline">
            {t.auth.resendLink}
          </Link>
        </>
      )}
      {(confirm.isPending || confirm.isIdle) && (
        <p role="status" className="text-sm text-muted-foreground">
          {t.auth.confirming}
        </p>
      )}
    </AuthLayout>
  );
}
