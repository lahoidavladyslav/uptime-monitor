import { Link, useLocation } from "react-router-dom";

import { t } from "@/lib/strings";

import { AuthLayout } from "./AuthLayout";

export function SignupSentPage() {
  const email = (useLocation().state as { email?: string } | null)?.email;
  return (
    <AuthLayout title={t.auth.sentTitle}>
      {email && (
        <p className="mb-3 text-sm">
          {t.auth.sentBody} <strong>{email}</strong>.
        </p>
      )}
      <Link to="/resend" className="text-sm text-accent underline">
        {t.auth.sentNoMail}
      </Link>
    </AuthLayout>
  );
}
