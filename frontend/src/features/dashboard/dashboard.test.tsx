import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";

import { server } from "@/test/server";
import { mockAuthed, renderApp } from "@/test/utils";

type Row = {
  id: number;
  name: string;
  status: "up" | "down" | "unknown" | "maintenance" | "paused";
  last_checked_at: string | null;
  uptime_24h: number | null;
  down_minutes: number | null;
  spark: (number | null)[];
};

const NONE = Array<number | null>(24).fill(null);
const row = (over: Partial<Row> & { id: number; name: string }): Row => ({
  status: "up",
  last_checked_at: new Date(Date.now() - (5 * 60_000 + 3_000)).toISOString(),
  uptime_24h: 99.9,
  down_minutes: null,
  spark: NONE.map((_, i) => 100 + i),
  ...over,
});

function summaryOf(rows: Row[]) {
  const c = { up: 0, down: 0, unknown: 0, maintenance: 0, paused: 0, open_incidents: 0 };
  rows.forEach((r) => {
    c[r.status] += 1;
    if (r.down_minutes !== null) c.open_incidents += 1;
  });
  return c;
}

function mockDashboard(rows: Row[]) {
  mockAuthed();
  const state = { calls: 0 };
  server.use(
    http.get("*/api/v1/dashboard", () => {
      state.calls += 1;
      return HttpResponse.json({ summary: summaryOf(rows), monitors: rows });
    }),
  );
  return state;
}

describe("dashboard", () => {
  it("renders the summary and one row per monitor with badge, sparkline, uptime and relative time", async () => {
    mockDashboard([row({ id: 1, name: "Сайт" }), row({ id: 2, name: "API", status: "down", uptime_24h: 50, down_minutes: 42 })]);
    const { container } = renderApp("/");
    await screen.findByText("Сайт");
    expect(container.querySelector('[data-stat="up"]')).toHaveTextContent("1");
    expect(container.querySelector('[data-stat="down"]')).toHaveTextContent("1");
    expect(container.querySelector('[data-stat="incidents"]')).toHaveTextContent("1");
    const first = screen.getByText("Сайт").closest("a")!;
    expect(within(first).getByText("Працює")).toBeInTheDocument();
    expect(within(first).getByText("99.9%")).toBeInTheDocument();
    expect(within(first).getByText("5 хв тому")).toBeInTheDocument();
    expect(first.querySelector("svg path")).not.toBeNull(); // sparkline
    expect(first).toHaveAttribute("href", "/monitors/1");
  });

  it("shows how long a monitor has been down", async () => {
    mockDashboard([row({ id: 2, name: "API", status: "down", down_minutes: 42 })]);
    renderApp("/");
    expect(await screen.findByText("Down вже 42 хв")).toBeInTheDocument();
  });

  it("a brand-new user sees an empty state with an add action", async () => {
    mockDashboard([]);
    renderApp("/");
    expect(await screen.findByText("Моніторів ще немає")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Додати монітор" })).toHaveAttribute("href", "/monitors/new");
  });

  it("null uptime shows an em dash and an empty sparkline — never NaN", async () => {
    mockDashboard([row({ id: 1, name: "Новий", status: "unknown", uptime_24h: null, last_checked_at: null, spark: NONE })]);
    const { container } = renderApp("/");
    const link = (await screen.findByText("Новий")).closest("a")!;
    expect(within(link).getAllByText("—").length).toBeGreaterThanOrEqual(2); // uptime + last check
    expect(within(link).getByTestId("sparkline-empty")).toBeInTheDocument();
    expect(container.textContent).not.toMatch(/NaN|undefined|null/);
  });

  it("search is case-insensitive and unicode-safe; the status filter narrows too", async () => {
    mockDashboard([row({ id: 1, name: "Магазин" }), row({ id: 2, name: "API", status: "down", down_minutes: 5 }), row({ id: 3, name: "Блог" })]);
    renderApp("/");
    await screen.findByText("Магазин");
    await userEvent.type(screen.getByLabelText("Пошук моніторів"), "МАГ");
    expect(screen.getByText("Магазин")).toBeInTheDocument();
    expect(screen.queryByText("API")).not.toBeInTheDocument();
    await userEvent.clear(screen.getByLabelText("Пошук моніторів"));
    await userEvent.selectOptions(screen.getByLabelText("Статус"), "down");
    expect(screen.getByText("API")).toBeInTheDocument();
    expect(screen.queryByText("Блог")).not.toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Пошук моніторів"), "zzz");
    expect(await screen.findByText("Нічого не знайдено")).toBeInTheDocument();
  });

  it("very long, emoji and right-to-left names are truncated without breaking the row", async () => {
    const long = "A".repeat(200);
    const rtl = "مرحبا بالعالم 🚀 " + "ב".repeat(60);
    mockDashboard([row({ id: 1, name: long }), row({ id: 2, name: rtl, status: "down", down_minutes: 3 })]);
    renderApp("/");
    const a = await screen.findByText(long);
    expect(a).toHaveClass("truncate");
    expect(a).toHaveAttribute("title", long);
    const b = screen.getByText(rtl);
    expect(b).toHaveClass("truncate");
    expect(b.closest("a")!).toHaveTextContent("Недоступний"); // badge still there
    expect(b).toHaveAttribute("dir", "auto");
  });

  it("shows skeletons while loading, an error with Retry on a 500, then recovers", async () => {
    mockAuthed();
    let fail = true;
    server.use(
      http.get("*/api/v1/dashboard", () =>
        fail ? new HttpResponse(null, { status: 500 }) : HttpResponse.json({ summary: summaryOf([row({ id: 1, name: "Сайт" })]), monitors: [row({ id: 1, name: "Сайт" })] }),
      ),
    );
    renderApp("/");
    expect(await screen.findByRole("alert")).toHaveTextContent("Сервер тимчасово недоступний");
    fail = false;
    await userEvent.click(screen.getByRole("button", { name: "Повторити" }));
    expect(await screen.findByText("Сайт")).toBeInTheDocument();
  });

  it("session expiry during polling: ONE redirect to login, no further requests", async () => {
    mockAuthed();
    let calls = 0;
    server.use(
      http.get("*/api/v1/dashboard", () => {
        calls += 1;
        return HttpResponse.json({ message: "Потрібен вхід.", errors: {} }, { status: 401 });
      }),
    );
    const { router } = renderApp("/");
    await waitFor(() => expect(router.state.location.pathname).toBe("/login"));
    expect(router.state.location.search).toBe("?next=%2F");
    await new Promise((r) => setTimeout(r, 300));
    expect(calls).toBe(1); // no retry storm, no zombie polling
  });
});

describe("dashboard polling", () => {
  afterEach(() => {
    vi.useRealTimers();
    Object.defineProperty(document, "visibilityState", { value: "visible", configurable: true });
  });

  function setVisibility(state: "visible" | "hidden") {
    Object.defineProperty(document, "visibilityState", { value: state, configurable: true });
    document.dispatchEvent(new Event("visibilitychange"));
  }

  it("polls every 10 s while visible and pauses while the tab is hidden", async () => {
    const state = mockDashboard([row({ id: 1, name: "Сайт" })]);
    vi.useFakeTimers({ shouldAdvanceTime: true });
    renderApp("/");
    await screen.findByText("Сайт");
    expect(state.calls).toBe(1);

    await vi.advanceTimersByTimeAsync(10_100);
    await waitFor(() => expect(state.calls).toBe(2));

    setVisibility("hidden");
    await vi.advanceTimersByTimeAsync(35_000);
    expect(state.calls).toBe(2); // nothing fired in the background

    setVisibility("visible");
    await vi.advanceTimersByTimeAsync(10_100);
    await waitFor(() => expect(state.calls).toBeGreaterThanOrEqual(3));
  });
});
