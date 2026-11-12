import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";

import { server } from "@/test/server";
import { mockAuthed, renderApp } from "@/test/utils";

const BASE = "*/api/v1/monitors/7";

const detail = (over = {}) => ({
  id: 7,
  name: "Мій сайт",
  url: "https://example.com",
  status: "up",
  is_active: true,
  last_checked_at: new Date(Date.now() - 120_000).toISOString(),
  ssl_days_left: 45,
  ssl_error: "",
  down_minutes: null,
  uptime: { h24: 99.95, d7: 98.5, d30: 97, d90: null },
  regions: [
    { region: "eu", status: "up", last_checked_at: new Date().toISOString() },
    { region: "us", status: "down", last_checked_at: new Date().toISOString() },
    { region: "asia", status: "stale", last_checked_at: null },
  ],
  ...over,
});
const stats = (over = {}) => ({ total: 100, up: 99, down: 1, uptime: 99, rt_avg: 120, rt_min: 80, rt_max: 400, ...over });
const check = (id: number, up = true) => ({
  id,
  checked_at: new Date(Date.now() - id * 60_000).toISOString(),
  status_code: up ? 200 : 503,
  response_time_ms: up ? 100 + id : null,
  is_up: up,
  error: up ? null : "HTTP 503",
});
const notFound = () => HttpResponse.json({ message: "Не знайдено.", errors: {} }, { status: 404 });

/** Mounts every endpoint of the page; tests override single routes with server.use afterwards. */
function mockApi(opts: { detail?: object; windows?: object[] } = {}) {
  mockAuthed();
  const seen = { stats: [] as string[], checks: [] as string[], post: [] as string[], del: [] as string[], maintenanceBodies: [] as unknown[] };
  server.use(
    http.get(BASE, () => HttpResponse.json(detail(opts.detail))),
    http.get(`${BASE}/stats`, ({ request }) => {
      seen.stats.push(new URL(request.url).search);
      return HttpResponse.json(stats());
    }),
    http.get(`${BASE}/series`, () => HttpResponse.json([{ t: "10:00", ms: 100, down: false }, { t: "10:01", ms: null, down: true }])),
    http.get(`${BASE}/checks`, ({ request }) => {
      const url = new URL(request.url);
      seen.checks.push(url.search);
      const failed = url.searchParams.get("only_failed") === "true";
      const page = Number(url.searchParams.get("page") ?? 1);
      const items = failed ? [check(3, false)] : page === 2 ? [check(21)] : [check(1), check(2, false)];
      return HttpResponse.json({ items, total: failed ? 1 : 21, page, pages: failed ? 1 : 2 });
    }),
    http.get(`${BASE}/incidents`, () =>
      HttpResponse.json([
        { id: 2, started_at: "2026-11-12T10:00:00Z", resolved_at: null, first_error: "timeout", minutes: 42 },
        { id: 1, started_at: "2026-11-11T10:00:00Z", resolved_at: "2026-11-11T11:30:00Z", first_error: "HTTP 500", minutes: 90 },
      ]),
    ),
    http.get(`${BASE}/maintenance`, () => HttpResponse.json(opts.windows ?? [])),
    http.post(`${BASE}/check`, () => {
      seen.post.push("check");
      return HttpResponse.json({ status: "queued" }, { status: 202 });
    }),
    http.post(`${BASE}/maintenance`, async ({ request }) => {
      seen.maintenanceBodies.push(await request.json());
      return HttpResponse.json({ id: 9, starts_at: "2030-01-01T10:00:00Z", ends_at: "2030-01-01T12:00:00Z", reason: "x" }, { status: 201 });
    }),
    http.delete(`${BASE}/maintenance/5`, () => {
      seen.del.push("5");
      return HttpResponse.json({ status: "deleted" });
    }),
  );
  return seen;
}

describe("monitor page: header and overview", () => {
  it("shows name, url, status, last check, uptime cards, stats and SSL", async () => {
    mockApi();
    renderApp("/monitors/7");
    expect(await screen.findByRole("heading", { name: "Мій сайт" })).toBeInTheDocument();
    expect(screen.getByText("https://example.com")).toBeInTheDocument();
    expect(screen.getAllByText("Працює").length).toBeGreaterThan(0);
    expect(await screen.findByText(/2 хв тому/)).toBeInTheDocument();
    const cards = screen.getByTestId("uptime-cards");
    expect(within(cards).getByText("99.9%")).toBeInTheDocument(); // truncated, never rounded up to 100
    expect(within(cards).getByText("98.5%")).toBeInTheDocument();
    expect(within(cards).getByText("97%")).toBeInTheDocument();
    expect(within(cards).getByText("—")).toBeInTheDocument(); // 90d: no data
    expect(await screen.findByText("120 мс")).toBeInTheDocument(); // average
    expect(screen.getByText(/45 дн/)).toBeInTheDocument();
    expect(await screen.findByRole("img", { name: /Час відповіді/ })).toBeInTheDocument();
  });

  it("shows what every region sees, and calls out a silent one instead of hiding it", async () => {
    mockApi();
    renderApp("/monitors/7");
    const section = (await screen.findByRole("heading", { name: "Регіони" })).closest("section")!;
    expect(within(section).getByText(/Працює/).closest("li")).toHaveAttribute("data-region", "eu");
    expect(within(section).getByText(/Недоступний/).closest("li")).toHaveAttribute("data-region", "us");
    expect(within(section).getByText(/Немає даних/).closest("li")).toHaveAttribute("data-region", "asia");
    expect(within(section).getByText(/Регіон мовчить і не враховується/)).toBeInTheDocument();
  });

  it("a single-region deployment shows no region card", async () => {
    mockApi({ detail: { regions: [{ region: "eu", status: "up", last_checked_at: null }] } });
    renderApp("/monitors/7");
    await screen.findByRole("heading", { name: "Мій сайт" });
    expect(screen.queryByRole("heading", { name: "Регіони" })).not.toBeInTheDocument();
  });

  it("a down monitor shows how long it has been down", async () => {
    mockApi({ detail: { status: "down", down_minutes: 42 } });
    renderApp("/monitors/7");
    expect(await screen.findByText("Down вже 42 хв")).toBeInTheDocument();
  });

  it("without any data stats read 'немає даних' instead of NaN or 0%", async () => {
    mockApi({ detail: { status: "unknown", last_checked_at: null, ssl_days_left: null, uptime: { h24: null, d7: null, d30: null, d90: null } } });
    server.use(http.get(`${BASE}/stats`, () => HttpResponse.json(stats({ total: 0, up: 0, down: 0, uptime: null, rt_avg: null, rt_min: null, rt_max: null }))));
    server.use(http.get(`${BASE}/series`, () => HttpResponse.json([])));
    renderApp("/monitors/7");
    expect(await screen.findByText("Даних для графіка ще немає")).toBeInTheDocument();
    expect(screen.queryByText(/NaN|undefined/)).not.toBeInTheDocument();
  });

  it("switching the period refetches stats for 7d", async () => {
    const seen = mockApi();
    renderApp("/monitors/7");
    await screen.findByText("120 мс");
    await userEvent.click(screen.getByRole("button", { name: "7 днів" }));
    await waitFor(() => expect(seen.stats.some((s) => s.includes("period=7d"))).toBe(true));
  });

  it("a foreign or missing monitor shows a not-found state with a way back", async () => {
    mockAuthed();
    server.use(http.get(BASE, notFound));
    renderApp("/monitors/7");
    expect(await screen.findByText("Монітор не знайдено")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "До моніторів" })).toHaveAttribute("href", "/");
  });

  it("a network failure offers a retry", async () => {
    mockAuthed();
    let calls = 0;
    server.use(
      http.get(BASE, () => {
        calls += 1;
        return calls <= 3 ? HttpResponse.error() : HttpResponse.json(detail());
      }),
    );
    renderApp("/monitors/7");
    await userEvent.click(await screen.findByRole("button", { name: "Повторити" }));
    expect(await screen.findByRole("heading", { name: "Мій сайт" })).toBeInTheDocument();
  });

  it("/monitors/new is not treated as a monitor id", async () => {
    mockAuthed();
    renderApp("/monitors/new");
    expect(await screen.findByText("Новий монітор")).toBeInTheDocument();
  });

  it("a non-numeric id is the not-found page, and no request is made", async () => {
    mockAuthed();
    renderApp("/monitors/abc");
    expect(await screen.findByText("Сторінку не знайдено")).toBeInTheDocument();
  });
});

describe("check now", () => {
  it("queues a check and confirms", async () => {
    const seen = mockApi();
    renderApp("/monitors/7");
    await userEvent.click(await screen.findByRole("button", { name: "Перевірити зараз" }));
    expect(await screen.findByText("Перевірку запущено")).toBeInTheDocument();
    expect(seen.post).toEqual(["check"]);
  });

  it.each([
    [429, "Зачекайте кілька секунд перед наступною перевіркою."],
    [409, "Монітор зараз на обслуговуванні — перевірки призупинено."],
  ])("shows the server's message for %i", async (status, message) => {
    mockApi();
    server.use(http.post(`${BASE}/check`, () => HttpResponse.json({ message, errors: {} }, { status })));
    renderApp("/monitors/7");
    await userEvent.click(await screen.findByRole("button", { name: "Перевірити зараз" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(message);
  });

  it("the button is disabled while the request is in flight (no double queueing)", async () => {
    const seen = mockApi();
    renderApp("/monitors/7");
    const button = await screen.findByRole("button", { name: "Перевірити зараз" });
    await userEvent.dblClick(button);
    await screen.findByText("Перевірку запущено");
    expect(seen.post).toHaveLength(1);
  });
});

describe("checks tab", () => {
  it("lists checks and paginates", async () => {
    const seen = mockApi();
    renderApp("/monitors/7?tab=checks");
    const table = await screen.findByRole("table");
    expect(within(table).getAllByRole("row")).toHaveLength(3); // header + 2
    expect(within(table).getByText("HTTP 503")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Далі" }));
    await waitFor(() => expect(seen.checks.some((s) => s.includes("page=2"))).toBe(true));
    expect(await screen.findByText("Сторінка 2 з 2")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Далі" })).toBeDisabled();
  });

  it("'лише падіння' filters on the server and resets to page 1", async () => {
    const seen = mockApi();
    renderApp("/monitors/7?tab=checks");
    await screen.findByRole("table");
    await userEvent.click(screen.getByRole("button", { name: "Далі" }));
    await screen.findByText("Сторінка 2 з 2");
    await userEvent.click(screen.getByRole("checkbox", { name: "Лише падіння" }));
    await waitFor(() => expect(seen.checks.at(-1)).toContain("only_failed=true"));
    expect(seen.checks.at(-1)).toContain("page=1");
  });

  it("empty history has an explanation", async () => {
    mockApi();
    server.use(http.get(`${BASE}/checks`, () => HttpResponse.json({ items: [], total: 0, page: 1, pages: 1 })));
    renderApp("/monitors/7?tab=checks");
    expect(await screen.findByText("Перевірок ще не було")).toBeInTheDocument();
  });
});

describe("incidents tab", () => {
  it("marks the open one as ongoing and shows duration of resolved ones", async () => {
    mockApi();
    renderApp("/monitors/7?tab=incidents");
    expect(await screen.findByText("триває")).toBeInTheDocument();
    expect(screen.getByText("1 год 30 хв")).toBeInTheDocument();
    expect(screen.getByText("timeout")).toBeInTheDocument();
  });

  it("no incidents is a positive empty state", async () => {
    mockApi();
    server.use(http.get(`${BASE}/incidents`, () => HttpResponse.json([])));
    renderApp("/monitors/7?tab=incidents");
    expect(await screen.findByText("Інцидентів не було")).toBeInTheDocument();
  });
});

describe("tabs", () => {
  it("switching tabs updates the URL so the view can be shared and survives reload", async () => {
    mockApi();
    const { router } = renderApp("/monitors/7");
    await userEvent.click(await screen.findByRole("tab", { name: "Інциденти" }));
    expect(router.state.location.search).toBe("?tab=incidents");
    expect(screen.getByRole("tab", { name: "Інциденти" })).toHaveAttribute("aria-selected", "true");
  });

  it("an unknown tab falls back to the overview", async () => {
    mockApi();
    renderApp("/monitors/7?tab=bogus");
    expect(await screen.findByTestId("uptime-cards")).toBeInTheDocument();
  });
});

describe("maintenance", () => {
  const WINDOW = { id: 5, starts_at: "2030-01-01T10:00:00Z", ends_at: "2030-01-01T12:00:00Z", reason: "Оновлення БД" };

  it("lists planned windows and deletes one", async () => {
    const seen = mockApi({ windows: [WINDOW] });
    renderApp("/monitors/7");
    expect(await screen.findByText("Оновлення БД")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Видалити вікно" }));
    await waitFor(() => expect(seen.del).toEqual(["5"]));
  });

  it("creates a window, sending UTC ISO timestamps", async () => {
    const seen = mockApi();
    renderApp("/monitors/7");
    await screen.findByRole("heading", { name: "Обслуговування" });
    await userEvent.type(screen.getByLabelText("Початок"), "2030-01-01T10:00");
    await userEvent.type(screen.getByLabelText("Кінець"), "2030-01-01T12:00");
    await userEvent.type(screen.getByLabelText("Причина"), "Оновлення");
    await userEvent.click(screen.getByRole("button", { name: "Запланувати" }));
    await waitFor(() => expect(seen.maintenanceBodies).toHaveLength(1));
    const body = seen.maintenanceBodies[0] as { starts_at: string; ends_at: string; reason: string };
    expect(body.reason).toBe("Оновлення");
    expect(body.starts_at).toMatch(/^2030-01-01T\d\d:00:00\.000Z$/);
    expect(new Date(body.ends_at).getTime() - new Date(body.starts_at).getTime()).toBe(2 * 3600_000);
  });

  it("shows the API's validation message next to the fields", async () => {
    mockApi();
    server.use(
      http.post(`${BASE}/maintenance`, () =>
        HttpResponse.json({ message: "Перевірте введені дані.", errors: { __all__: ["Кінець має бути пізніше за початок."] } }, { status: 422 }),
      ),
    );
    renderApp("/monitors/7");
    await screen.findByRole("heading", { name: "Обслуговування" });
    await userEvent.type(screen.getByLabelText("Початок"), "2030-01-01T12:00");
    await userEvent.type(screen.getByLabelText("Кінець"), "2030-01-01T10:00");
    await userEvent.click(screen.getByRole("button", { name: "Запланувати" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Кінець має бути пізніше за початок.");
  });

  it("does not submit an empty form", async () => {
    const seen = mockApi();
    renderApp("/monitors/7");
    await screen.findByRole("heading", { name: "Обслуговування" });
    await userEvent.click(screen.getByRole("button", { name: "Запланувати" }));
    expect((await screen.findAllByText("Обов'язкове поле")).length).toBeGreaterThan(0);
    expect(seen.maintenanceBodies).toHaveLength(0);
  });
});
