import { Link } from "react-router-dom";

import { t } from "@/lib/strings";

export function NotFoundPage() {
  return (
    <div className="mx-auto flex max-w-md flex-col items-center gap-3 p-16 text-center">
      <p className="text-5xl font-semibold text-muted-foreground">404</p>
      <h1 className="text-lg font-medium">{t.notFound.title}</h1>
      <Link to="/" className="text-accent underline">
        {t.notFound.home}
      </Link>
    </div>
  );
}
