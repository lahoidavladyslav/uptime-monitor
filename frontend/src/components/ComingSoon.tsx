import { t } from "@/lib/strings";

import { EmptyState } from "./States";

export function ComingSoon({ title }: { title: string }) {
  return <EmptyState title={title} description={t.common.comingSoon} />;
}
