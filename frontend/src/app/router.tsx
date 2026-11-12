import { createBrowserRouter, createMemoryRouter, type RouteObject } from "react-router-dom";

import { ComingSoon } from "@/components/ComingSoon";
import { NotFoundPage } from "@/components/NotFoundPage";
import { ConfirmPage } from "@/features/auth/ConfirmPage";
import { DashboardPage } from "@/features/dashboard/DashboardPage";
import { ForgotPasswordPage } from "@/features/auth/ForgotPasswordPage";
import { LoginPage } from "@/features/auth/LoginPage";
import { PublicOnly } from "@/features/auth/PublicOnly";
import { RequireAuth } from "@/features/auth/RequireAuth";
import { ResendPage } from "@/features/auth/ResendPage";
import { ResetPasswordPage } from "@/features/auth/ResetPasswordPage";
import { SignupPage } from "@/features/auth/SignupPage";
import { SignupSentPage } from "@/features/auth/SignupSentPage";
import { t } from "@/lib/strings";

// Screens still to come are <ComingSoon/> stubs; later tasks/stages replace them route by route.
const Stub = ({ name }: { name: string }) => <ComingSoon title={name} />;

export const routes: RouteObject[] = [
  {
    element: <PublicOnly />,
    children: [
      { path: "/login", element: <LoginPage /> },
      { path: "/signup", element: <SignupPage /> },
      { path: "/signup/sent", element: <SignupSentPage /> },
      { path: "/forgot-password", element: <ForgotPasswordPage /> },
    ],
  },
  { path: "/confirm/:token", element: <ConfirmPage /> },
  { path: "/resend", element: <ResendPage /> },
  { path: "/reset-password/:uid/:token", element: <ResetPasswordPage /> },
  {
    element: <RequireAuth />,
    children: [
      { path: "/", element: <DashboardPage /> },
      { path: "/monitors/:id", element: <Stub name="Монітор" /> },
      { path: "/status-pages", element: <Stub name={t.nav.statusPages} /> },
      { path: "/reports", element: <Stub name={t.nav.reports} /> },
      { path: "/security", element: <Stub name={t.nav.security} /> },
    ],
  },
  { path: "*", element: <NotFoundPage /> },
];

const future = {
  v7_relativeSplatPath: true,
  v7_fetcherPersist: true,
  v7_normalizeFormMethod: true,
  v7_partialHydration: true,
  v7_skipActionErrorRevalidation: true,
};

/** Browser router for the app; pass `initialEntries` to get an in-memory router (tests). */
export function createAppRouter(initialEntries?: string[]) {
  return initialEntries ? createMemoryRouter(routes, { initialEntries, future }) : createBrowserRouter(routes, { future });
}
