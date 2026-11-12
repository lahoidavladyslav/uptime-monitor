import { zodResolver } from "@hookform/resolvers/zod";
import { useState } from "react";
import { useForm, useWatch } from "react-hook-form";
import { Link, useNavigate, useParams } from "react-router-dom";
import { z } from "zod";

import { ApiError } from "@/api/errors";
import {
  FormAlert,
  FormCheckbox,
  FormField,
  FormSelect,
  FormTextarea,
} from "@/components/FormField";
import { NotFoundPage } from "@/components/NotFoundPage";
import { EmptyState, ErrorState, ListSkeleton } from "@/components/States";
import { Button, buttonVariants } from "@/components/ui/button";
import { applyApiErrors } from "@/lib/forms";
import { t } from "@/lib/strings";
import { cn } from "@/lib/utils";

import {
  useDeleteMonitor,
  useMonitorConfig,
  useSaveMonitor,
  type MonitorInput,
} from "./useMonitorForm";

const required = z.string().trim().min(1, t.form.required);
const schema = z.object({
  name: required,
  url: required,
  interval_seconds: z.string(),
  is_active: z.boolean(),
  http_method: z.enum(["GET", "HEAD", "POST"]),
  expected_status: z.string(),
  request_headers: z.string(),
  request_body: z.string(),
  expected_text: z.string(),
  is_public: z.boolean(),
  public_title: z.string(),
  public_logo_url: z.string(),
});
type Values = z.infer<typeof schema>;

const FIELDS = Object.keys(schema.shape);
const ADVANCED = ["request_headers", "request_body", "expected_text"];
const DEFAULTS: Values = {
  name: "",
  url: "",
  interval_seconds: "300",
  is_active: true,
  http_method: "GET",
  expected_status: "",
  request_headers: "",
  request_body: "",
  expected_text: "",
  is_public: false,
  public_title: "",
  public_logo_url: "",
};

function toBody(v: Values): MonitorInput {
  return { ...v, interval_seconds: Number(v.interval_seconds) };
}

export function MonitorFormPage() {
  const { id: raw } = useParams();
  if (raw === undefined) return <MonitorForm id={null} />; // /monitors/new
  const id = /^[1-9]\d*$/.test(raw) ? Number(raw) : null;
  return id === null ? <NotFoundPage /> : <EditLoader id={id} />;
}

function EditLoader({ id }: { id: number }) {
  const config = useMonitorConfig(id);
  if (config.isPending) return <ListSkeleton rows={4} />;
  if (config.isError) {
    if (config.error instanceof ApiError && config.error.status === 404) {
      return (
        <EmptyState
          title={t.monitor.notFoundTitle}
          description={t.monitor.notFoundBody}
          action={
            <Link
              to="/"
              className={cn(buttonVariants({ variant: "outline" }), "mt-2")}
            >
              {t.monitor.backToList}
            </Link>
          }
        />
      );
    }
    return (
      <ErrorState
        message={config.error.message}
        onRetry={() => void config.refetch()}
      />
    );
  }
  const c = config.data;
  return (
    <MonitorForm
      id={id}
      initial={{ ...c, interval_seconds: String(c.interval_seconds) } as Values}
    />
  );
}

function MonitorForm({ id, initial }: { id: number | null; initial?: Values }) {
  const navigate = useNavigate();
  const save = useSaveMonitor(id);
  const remove = useDeleteMonitor(id ?? 0);
  const [confirming, setConfirming] = useState(false);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const { register, handleSubmit, setError, control, formState } =
    useForm<Values>({
      resolver: zodResolver(schema),
      defaultValues: initial ?? DEFAULTS,
    });
  const { errors } = formState;
  const method = useWatch({ control, name: "http_method" });
  const isPublic = useWatch({ control, name: "is_public" });
  const errorCount = Object.keys(errors).filter((k) => k !== "root").length;

  // a rejected field inside the collapsed section must not stay hidden
  const advancedForced = ADVANCED.some((f) => f in errors);

  const submit = handleSubmit((values) => {
    if (save.isPending) return; // a double click must not create two monitors
    save.mutate(toBody(values), {
      onSuccess: (saved) => navigate(`/monitors/${saved.id}`),
      onError: (e) => {
        applyApiErrors(e, setError, FIELDS);
      },
    });
  });

  return (
    <form
      onSubmit={submit}
      noValidate
      className="mx-auto flex max-w-2xl flex-col gap-6"
    >
      <h1 className="text-2xl font-semibold">
        {id === null ? t.monitorForm.newTitle : t.monitorForm.editTitle}
      </h1>
      {errorCount > 0 && (
        <div role="status" className="text-sm font-medium text-status-down">
          {t.monitorForm.fixFields(errorCount)}
        </div>
      )}
      <FormAlert message={errors.root?.message} />

      <fieldset className="flex flex-col gap-4 rounded-lg border border-border bg-card p-4">
        <legend className="px-1 text-sm font-semibold">
          {t.monitorForm.basics}
        </legend>
        <FormField
          label={t.monitorForm.name}
          maxLength={100}
          error={errors.name?.message}
          {...register("name")}
        />
        <FormField
          label={t.monitorForm.url}
          type="url"
          inputMode="url"
          placeholder="https://example.com"
          error={errors.url?.message}
          {...register("url")}
        />
        <FormSelect
          label={t.monitorForm.interval}
          error={errors.interval_seconds?.message}
          {...register("interval_seconds")}
        >
          {Object.entries(t.monitorForm.intervals).map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </FormSelect>
        <FormCheckbox
          label={t.monitorForm.active}
          hint={t.monitorForm.activeHint}
          {...register("is_active")}
        />
      </fieldset>

      <fieldset className="flex flex-col gap-4 rounded-lg border border-border bg-card p-4">
        <legend className="px-1 text-sm font-semibold">
          {t.monitorForm.check}
        </legend>
        <div className="grid gap-4 sm:grid-cols-2">
          <FormSelect
            label={t.monitorForm.method}
            error={errors.http_method?.message}
            {...register("http_method")}
          >
            {["GET", "HEAD", "POST"].map((m) => (
              <option key={m}>{m}</option>
            ))}
          </FormSelect>
          <FormField
            label={t.monitorForm.expectedStatus}
            hint={t.monitorForm.expectedStatusHint}
            error={errors.expected_status?.message}
            {...register("expected_status")}
          />
        </div>
        <details
          open={advancedOpen || advancedForced}
          onToggle={(e) => setAdvancedOpen(e.currentTarget.open)}
        >
          <summary className="cursor-pointer text-sm font-medium">
            {t.monitorForm.advanced}
          </summary>
          <div className="mt-3 flex flex-col gap-4">
            <FormTextarea
              label={t.monitorForm.headers}
              hint={t.monitorForm.headersHint}
              placeholder={"Authorization: Bearer …\nAccept: application/json"}
              error={errors.request_headers?.message}
              {...register("request_headers")}
            />
            <FormTextarea
              label={t.monitorForm.body}
              hint={t.monitorForm.bodyHint}
              disabled={method !== "POST"}
              error={errors.request_body?.message}
              {...register("request_body")}
            />
            <FormField
              label={t.monitorForm.expectedText}
              hint={t.monitorForm.expectedTextHint}
              disabled={method === "HEAD"}
              error={errors.expected_text?.message}
              {...register("expected_text")}
            />
          </div>
        </details>
      </fieldset>

      <fieldset className="flex flex-col gap-4 rounded-lg border border-border bg-card p-4">
        <legend className="px-1 text-sm font-semibold">
          {t.monitorForm.publicity}
        </legend>
        <FormCheckbox
          label={t.monitorForm.isPublic}
          hint={t.monitorForm.isPublicHint}
          {...register("is_public")}
        />
        <FormField
          label={t.monitorForm.publicTitle}
          disabled={!isPublic}
          maxLength={100}
          error={errors.public_title?.message}
          {...register("public_title")}
        />
        <FormField
          label={t.monitorForm.publicLogo}
          type="url"
          disabled={!isPublic}
          error={errors.public_logo_url?.message}
          {...register("public_logo_url")}
        />
      </fieldset>

      <div className="flex flex-wrap items-center gap-3">
        <Button type="submit" disabled={save.isPending}>
          {save.isPending
            ? t.monitorForm.saving
            : id === null
              ? t.monitorForm.create
              : t.monitorForm.save}
        </Button>
        <Link
          to={id === null ? "/" : `/monitors/${id}`}
          className={cn(buttonVariants({ variant: "ghost" }))}
        >
          {t.monitorForm.cancel}
        </Link>
        {id !== null && (
          <Button
            type="button"
            variant="destructive"
            className="ml-auto"
            onClick={() => setConfirming(true)}
          >
            {t.monitorForm.delete}
          </Button>
        )}
      </div>

      {confirming && (
        <div
          role="alertdialog"
          aria-labelledby="del-title"
          aria-describedby="del-body"
          className="flex flex-col gap-3 rounded-lg border border-status-down/50 bg-status-down/5 p-4"
        >
          <h2 id="del-title" className="font-semibold">
            {t.monitorForm.deleteTitle}
          </h2>
          <p id="del-body" className="text-sm">
            {t.monitorForm.deleteBody}
          </p>
          {remove.isError && (
            <p role="alert" className="text-sm text-status-down">
              {remove.error.message}
            </p>
          )}
          <div className="flex gap-2">
            <Button
              type="button"
              variant="destructive"
              disabled={remove.isPending}
              onClick={() =>
                remove.mutate(undefined, { onSuccess: () => navigate("/") })
              }
            >
              {t.monitorForm.deleteConfirm}
            </Button>
            <Button
              type="button"
              variant="outline"
              onClick={() => setConfirming(false)}
            >
              {t.monitorForm.deleteCancel}
            </Button>
          </div>
        </div>
      )}
    </form>
  );
}
