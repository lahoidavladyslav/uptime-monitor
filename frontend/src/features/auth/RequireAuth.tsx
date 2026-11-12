import { Navigate, Outlet, useLocation } from "react-router-dom";

import { AppShell } from "@/components/AppShell";
import { ErrorState, ListSkeleton } from "@/components/States";

import { useLogout } from "./useLogout";
import { useMe } from "./useMe";

export function RequireAuth() {
  const me = useMe();
  const logout = useLogout();
  const location = useLocation();

  if (me.isPending) return <div className="p-8"><ListSkeleton rows={3} /></div>;
  if (me.isError) return <div className="p-8"><ErrorState message={me.error.message} onRetry={() => void me.refetch()} /></div>;
  if (!me.data) return <Navigate to={`/login?next=${encodeURIComponent(location.pathname + location.search)}`} replace />;

  return (
    <AppShell username={me.data.username} onLogout={() => logout.mutate()}>
      <Outlet />
    </AppShell>
  );
}
