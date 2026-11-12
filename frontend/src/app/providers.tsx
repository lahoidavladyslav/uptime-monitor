import { QueryClientProvider, type QueryClient } from "@tanstack/react-query";
import { useEffect, useState, type ReactNode } from "react";

import { ThemeProvider } from "@/components/ThemeProvider";
import { ensureCsrf } from "@/lib/csrf";

import { createQueryClient } from "./queryClient";

export function AppProviders({ children, queryClient }: { children: ReactNode; queryClient?: QueryClient }) {
  const [client] = useState(() => queryClient ?? createQueryClient());
  useEffect(() => {
    void ensureCsrf(); // so the very first POST (login) already has its token
  }, []);
  return (
    <ThemeProvider>
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    </ThemeProvider>
  );
}
