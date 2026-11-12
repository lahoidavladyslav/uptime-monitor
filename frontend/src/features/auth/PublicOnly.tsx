import { Navigate, Outlet, useSearchParams } from "react-router-dom";

import { safeNext } from "@/lib/nav";

import { useMe } from "./useMe";

/** Login/signup pages: signed-in users are sent to the dashboard. While loading or on a network error
 *  the page is shown anyway (it has its own retry paths), so a flaky `me` never traps anyone. */
export function PublicOnly() {
  const me = useMe();
  const [params] = useSearchParams();
  if (me.data) return <Navigate to={safeNext(params.get("next")) ?? "/"} replace />;
  return <Outlet />;
}
