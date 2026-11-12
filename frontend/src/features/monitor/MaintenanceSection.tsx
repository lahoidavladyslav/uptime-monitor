import { zodResolver } from "@hookform/resolvers/zod";
import { Trash2 } from "lucide-react";
import { useForm } from "react-hook-form";
import { z } from "zod";

import { FormAlert, FormField } from "@/components/FormField";
import { Button } from "@/components/ui/button";
import { formatDateTime } from "@/lib/format";
import { applyApiErrors } from "@/lib/forms";
import { t } from "@/lib/strings";

import { useCreateWindow, useDeleteWindow, useMaintenance } from "./useMonitor";

const schema = z.object({
  starts_at: z.string().min(1, t.form.required),
  ends_at: z.string().min(1, t.form.required),
  reason: z.string().max(200),
});
type Values = z.infer<typeof schema>;
const FIELDS = ["starts_at", "ends_at", "reason"] as const;

export function MaintenanceSection({ monitorId }: { monitorId: number }) {
  const windows = useMaintenance(monitorId);
  const create = useCreateWindow(monitorId);
  const remove = useDeleteWindow(monitorId);
  const { register, handleSubmit, setError, reset, formState } = useForm<Values>({
    resolver: zodResolver(schema),
    defaultValues: { starts_at: "", ends_at: "", reason: "" },
  });
  const { errors } = formState;

  const submit = handleSubmit((v) => {
    if (create.isPending) return;
    // <input type="datetime-local"> is the user's local time; the server works in UTC
    const body = { starts_at: new Date(v.starts_at).toISOString(), ends_at: new Date(v.ends_at).toISOString(), reason: v.reason };
    create.mutate(body, { onSuccess: () => reset(), onError: (e) => applyApiErrors(e, setError, FIELDS) });
  });

  return (
    <section aria-labelledby="maint-title" className="flex flex-col gap-3">
      <div>
        <h2 id="maint-title" className="text-lg font-semibold">
          {t.monitor.maintenance.title}
        </h2>
        <p className="text-sm text-muted-foreground">{t.monitor.maintenance.hint}</p>
      </div>

      {windows.data?.length === 0 && <p className="text-sm text-muted-foreground">{t.monitor.maintenance.empty}</p>}
      <ul className="flex flex-col gap-2">
        {windows.data?.map((w) => (
          <li key={w.id} className="flex items-center justify-between gap-3 rounded-lg border border-border bg-card p-3 text-sm">
            <div className="min-w-0">
              <div>
                {formatDateTime(w.starts_at)} → {formatDateTime(w.ends_at)}
              </div>
              {w.reason && <div className="truncate text-muted-foreground">{w.reason}</div>}
            </div>
            <Button variant="ghost" size="icon" aria-label={t.monitor.maintenance.remove} disabled={remove.isPending} onClick={() => remove.mutate(w.id)}>
              <Trash2 aria-hidden="true" className="size-4" />
            </Button>
          </li>
        ))}
      </ul>

      <form onSubmit={submit} noValidate className="grid gap-3 rounded-lg border border-border bg-card p-4 md:grid-cols-3">
        <FormField type="datetime-local" label={t.monitor.maintenance.start} error={errors.starts_at?.message} {...register("starts_at")} />
        <FormField type="datetime-local" label={t.monitor.maintenance.end} error={errors.ends_at?.message} {...register("ends_at")} />
        <FormField label={t.monitor.maintenance.reason} maxLength={200} error={errors.reason?.message} {...register("reason")} />
        <div className="flex flex-col gap-2 md:col-span-3">
          <FormAlert message={errors.root?.message} />
          <Button type="submit" className="w-fit" disabled={create.isPending}>
            {t.monitor.maintenance.add}
          </Button>
        </div>
      </form>
    </section>
  );
}
