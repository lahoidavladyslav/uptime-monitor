import type { ReactNode } from "react";

import { Card } from "@/components/ui/card";
import { t } from "@/lib/strings";

export function AuthLayout({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="flex min-h-screen flex-col items-center justify-center gap-6 p-4">
      <div className="flex items-center gap-2 font-semibold">
        <span aria-hidden="true" className="inline-block size-3 rounded-full bg-accent" />
        {t.appName}
      </div>
      <Card className="w-full max-w-sm p-6">
        <h1 className="mb-4 text-xl font-semibold">{title}</h1>
        {children}
      </Card>
    </div>
  );
}
