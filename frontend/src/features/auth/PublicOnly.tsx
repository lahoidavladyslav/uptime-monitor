import { Navigate, Outlet } from "react-router-dom";

import { useMe } from "./useMe";

/** Login/signup pages: signed-in users are sent to the dashboard. While loading or on a network error
 *  the page is shown anyway (it has its own retry paths), so a flaky `me` never traps anyone. */
export function PublicOnly() {
  const me = useMe();
  if (me.data) return <Navigate to="/" replace />;
  return <Outlet />;
}
