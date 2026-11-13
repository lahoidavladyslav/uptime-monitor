import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";

import { server } from "@/test/server";
import { mockAuthed, renderApp } from "@/test/utils";

const CONFIG = {
  id: 7,
  name: "Мій сайт",
  url: "https://example.com",
  interval_seconds: 300,
  is_active: true,
  is_public: false,
  public_title: "",
  public_logo_url: "",
  http_method: "GET",
  request_headers: "Accept: application/json",
  request_body: "",
  check_type: "http_status",
  check_config: {},
  public_slug: null,
};

function mockApi() {
  mockAuthed();
  const seen = {
    posts: [] as Record<string, unknown>[],
    puts: [] as Record<string, unknown>[],
    deletes: 0,
  };
  server.use(
    http.get("*/api/v1/dashboard", () =>
      HttpResponse.json({
        summary: {
          up: 0,
          down: 0,
          unknown: 0,
          maintenance: 0,
          paused: 0,
          open_incidents: 0,
        },
        monitors: [],
      }),
    ),
    http.get("*/api/v1/monitors/7/config", () => HttpResponse.json(CONFIG)),
    http.get("*/api/v1/monitors/7", () =>
      HttpResponse.json({
        id: 7,
        name: "Мій сайт",
        url: "https://example.com",
        status: "up",
        is_active: true,
        last_checked_at: null,
        ssl_days_left: null,
        ssl_error: "",
        down_minutes: null,
        uptime: { h24: null, d7: null, d30: null, d90: null },
        regions: [],
      }),
    ),
    http.get("*/api/v1/monitors/7/stats", () =>
      HttpResponse.json({
        total: 0,
        up: 0,
        down: 0,
        uptime: null,
        rt_avg: null,
        rt_min: null,
        rt_max: null,
      }),
    ),
    http.get("*/api/v1/monitors/7/series", () => HttpResponse.json([])),
    http.get("*/api/v1/monitors/7/maintenance", () => HttpResponse.json([])),
    http.post("*/api/v1/monitors", async ({ request }) => {
      seen.posts.push((await request.json()) as Record<string, unknown>);
      return HttpResponse.json({ ...CONFIG, id: 7 }, { status: 201 });
    }),
    http.put("*/api/v1/monitors/7", async ({ request }) => {
      seen.puts.push((await request.json()) as Record<string, unknown>);
      return HttpResponse.json(CONFIG);
    }),
    http.delete("*/api/v1/monitors/7", () => {
      seen.deletes += 1;
      return HttpResponse.json({ status: "deleted" });
    }),
  );
  return seen;
}

const fill = async (label: string, value: string) => {
  const field = screen.getByLabelText(label);
  await userEvent.clear(field);
  await userEvent.type(field, value);
};

describe("create monitor", () => {
  it("sends the whole form and lands on the new monitor's page", async () => {
    const seen = mockApi();
    const { router } = renderApp("/monitors/new");
    await screen.findByRole("heading", { name: "Новий монітор" });
    await fill("Назва", "API");
    await fill("URL", "https://api.example.org");
    await userEvent.selectOptions(screen.getByLabelText("Інтервал"), "60");
    await userEvent.click(
      screen.getByRole("button", { name: "Створити монітор" }),
    );
    await waitFor(() =>
      expect(router.state.location.pathname).toBe("/monitors/7"),
    );
    expect(seen.posts[0]).toMatchObject({
      name: "API",
      url: "https://api.example.org",
      interval_seconds: 60,
      is_active: true,
      is_public: false,
      http_method: "GET",
    });
  });

  it("has sensible defaults: active, private, GET, every 5 minutes", async () => {
    mockApi();
    renderApp("/monitors/new");
    await screen.findByRole("heading", { name: "Новий монітор" });
    expect(screen.getByLabelText("Активний")).toBeChecked();
    expect(screen.getByLabelText("Публічна сторінка")).not.toBeChecked();
    expect(screen.getByLabelText("Метод")).toHaveValue("GET");
    expect(screen.getByLabelText("Інтервал")).toHaveValue("300");
  });

  it("does not submit while name or URL are empty", async () => {
    const seen = mockApi();
    renderApp("/monitors/new");
    await userEvent.click(
      await screen.findByRole("button", { name: "Створити монітор" }),
    );
    expect((await screen.findAllByText("Обов'язкове поле")).length).toBe(2);
    expect(seen.posts).toHaveLength(0);
  });

  it("shows the server's validation message next to the field and keeps what was typed", async () => {
    mockApi();
    server.use(
      http.post("*/api/v1/monitors", () =>
        HttpResponse.json(
          {
            message: "Перевірте введені дані.",
            errors: { url: ["Адреса веде у приватну/внутрішню мережу"] },
          },
          { status: 422 },
        ),
      ),
    );
    renderApp("/monitors/new");
    await screen.findByRole("heading", { name: "Новий монітор" });
    await fill("Назва", "Local");
    await fill("URL", "http://127.0.0.1");
    await userEvent.click(
      screen.getByRole("button", { name: "Створити монітор" }),
    );
    expect(
      await screen.findByText("Адреса веде у приватну/внутрішню мережу"),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("URL")).toHaveAttribute(
      "aria-invalid",
      "true",
    );
    expect(screen.getByLabelText("URL")).toHaveValue("http://127.0.0.1");
    expect(screen.getByRole("status")).toHaveTextContent(
      "Виправте виділене поле.",
    );
  });

  it("opens the collapsed advanced section when the server rejects a field inside it", async () => {
    mockApi();
    server.use(
      http.post("*/api/v1/monitors", () =>
        HttpResponse.json(
          {
            message: "Перевірте введені дані.",
            errors: { request_headers: ["Заголовок Host задавати не можна."] },
          },
          { status: 422 },
        ),
      ),
    );
    renderApp("/monitors/new");
    await screen.findByRole("heading", { name: "Новий монітор" });
    const advanced = screen
      .getByText("Розширені налаштування")
      .closest("details")!;
    expect(advanced).not.toHaveAttribute("open");
    await fill("Назва", "X");
    await fill("URL", "https://x.example.org");
    await userEvent.click(
      screen.getByRole("button", { name: "Створити монітор" }),
    );
    expect(
      await screen.findByText("Заголовок Host задавати не можна."),
    ).toBeInTheDocument();
    await waitFor(() => expect(advanced).toHaveAttribute("open"));
  });

  it("the request body is only for POST", async () => {
    mockApi();
    renderApp("/monitors/new");
    await screen.findByRole("heading", { name: "Новий монітор" });
    await userEvent.click(screen.getByText("Розширені налаштування"));
    expect(screen.getByLabelText("Тіло запиту")).toBeDisabled();
    await userEvent.selectOptions(screen.getByLabelText("Метод"), "POST");
    expect(screen.getByLabelText("Тіло запиту")).toBeEnabled();
  });

  it("the public fields only matter when the page is public", async () => {
    mockApi();
    renderApp("/monitors/new");
    await screen.findByRole("heading", { name: "Новий монітор" });
    expect(screen.getByLabelText("Назва на публічній сторінці")).toBeDisabled();
    await userEvent.click(screen.getByLabelText("Публічна сторінка"));
    expect(screen.getByLabelText("Назва на публічній сторінці")).toBeEnabled();
  });

  it("a double click on the button sends one request", async () => {
    const seen = mockApi();
    renderApp("/monitors/new");
    await screen.findByRole("heading", { name: "Новий монітор" });
    await fill("Назва", "A");
    await fill("URL", "https://a.example.org");
    await userEvent.dblClick(
      screen.getByRole("button", { name: "Створити монітор" }),
    );
    await waitFor(() => expect(seen.posts.length).toBeGreaterThan(0));
    expect(seen.posts).toHaveLength(1);
  });

  it("a network failure shows an error and keeps the form", async () => {
    mockApi();
    server.use(http.post("*/api/v1/monitors", () => HttpResponse.error()));
    renderApp("/monitors/new");
    await screen.findByRole("heading", { name: "Новий монітор" });
    await fill("Назва", "A");
    await fill("URL", "https://a.example.org");
    await userEvent.click(
      screen.getByRole("button", { name: "Створити монітор" }),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Немає з'єднання",
    );
    expect(screen.getByLabelText("Назва")).toHaveValue("A");
  });
});

describe("edit monitor", () => {
  it("is prefilled from the saved configuration", async () => {
    mockApi();
    renderApp("/monitors/7/edit");
    await screen.findByRole("heading", { name: "Редагувати монітор" });
    expect(screen.getByLabelText("Назва")).toHaveValue("Мій сайт");
    expect(screen.getByLabelText("URL")).toHaveValue("https://example.com");
    await userEvent.click(screen.getByText("Розширені налаштування"));
    expect(screen.getByLabelText("Заголовки")).toHaveValue(
      "Accept: application/json",
    );
  });

  it("saves with PUT and returns to the monitor page", async () => {
    const seen = mockApi();
    const { router } = renderApp("/monitors/7/edit");
    await screen.findByRole("heading", { name: "Редагувати монітор" });
    await fill("Назва", "Нова назва");
    await userEvent.click(screen.getByRole("button", { name: "Зберегти" }));
    await waitFor(() =>
      expect(router.state.location.pathname).toBe("/monitors/7"),
    );
    expect(seen.puts[0]).toMatchObject({
      name: "Нова назва",
      url: "https://example.com",
      request_headers: "Accept: application/json",
    });
  });

  it("a missing or foreign monitor shows not-found with a way back", async () => {
    mockAuthed();
    server.use(
      http.get("*/api/v1/monitors/7/config", () =>
        HttpResponse.json(
          { message: "Не знайдено.", errors: {} },
          { status: 404 },
        ),
      ),
    );
    renderApp("/monitors/7/edit");
    expect(await screen.findByText("Монітор не знайдено")).toBeInTheDocument();
  });

  it("deleting needs a confirmation, then removes the monitor and goes to the list", async () => {
    const seen = mockApi();
    const { router } = renderApp("/monitors/7/edit");
    await screen.findByRole("heading", { name: "Редагувати монітор" });
    await userEvent.click(
      screen.getByRole("button", { name: "Видалити монітор" }),
    );
    expect(seen.deletes).toBe(0); // first click only asks
    const dialog = screen.getByRole("alertdialog");
    expect(within(dialog).getByText(/разом з історією/)).toBeInTheDocument();
    await userEvent.click(
      within(dialog).getByRole("button", { name: "Видалити назавжди" }),
    );
    await waitFor(() => expect(router.state.location.pathname).toBe("/"));
    expect(seen.deletes).toBe(1);
  });

  it("cancelling the confirmation deletes nothing", async () => {
    const seen = mockApi();
    renderApp("/monitors/7/edit");
    await screen.findByRole("heading", { name: "Редагувати монітор" });
    await userEvent.click(
      screen.getByRole("button", { name: "Видалити монітор" }),
    );
    await userEvent.click(
      within(screen.getByRole("alertdialog")).getByRole("button", {
        name: "Скасувати",
      }),
    );
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(seen.deletes).toBe(0);
  });

  it("the monitor page offers an edit link", async () => {
    mockApi();
    renderApp("/monitors/7");
    expect(
      await screen.findByRole("link", { name: "Редагувати" }),
    ).toHaveAttribute("href", "/monitors/7/edit");
  });
});

describe("check types", () => {
  it("shows the settings of the chosen type and sends them as check_config", async () => {
    const seen = mockApi();
    renderApp("/monitors/new");
    await screen.findByRole("heading", { name: "Новий монітор" });
    expect(screen.queryByLabelText("Слово")).not.toBeInTheDocument();
    await userEvent.selectOptions(
      screen.getByLabelText("Що перевіряємо"),
      "keyword",
    );
    await fill("Назва", "K");
    await fill("URL", "https://k.example.org");
    await userEvent.type(screen.getByLabelText("Слово"), "Welcome");
    await userEvent.click(screen.getByLabelText("Слово має бути у відповіді")); // -> must NOT contain
    await userEvent.click(
      screen.getByRole("button", { name: "Створити монітор" }),
    );
    await waitFor(() => expect(seen.posts).toHaveLength(1));
    expect(seen.posts[0]).toMatchObject({
      check_type: "keyword",
      check_config: { keyword: "Welcome", must_contain: false },
    });
    expect(seen.posts[0]).not.toHaveProperty("expected_status");
  });

  it("json_path sends the path and a typed expected value", async () => {
    const seen = mockApi();
    renderApp("/monitors/new");
    await screen.findByRole("heading", { name: "Новий монітор" });
    await userEvent.selectOptions(
      screen.getByLabelText("Що перевіряємо"),
      "json_path",
    );
    await fill("Назва", "J");
    await fill("URL", "https://j.example.org");
    await userEvent.type(screen.getByLabelText("Шлях у JSON"), "status.db");
    await userEvent.type(screen.getByLabelText("Очікуване значення"), "ok");
    await userEvent.type(screen.getByLabelText("Дозволені коди"), "200,204");
    await userEvent.click(
      screen.getByRole("button", { name: "Створити монітор" }),
    );
    await waitFor(() => expect(seen.posts).toHaveLength(1));
    expect(seen.posts[0]).toMatchObject({
      check_type: "json_path",
      check_config: {
        path: "status.db",
        equals: "ok",
        expected_statuses: [200, 204],
      },
    });
  });

  it("ssl_expiry asks for days instead of HTTP statuses", async () => {
    const seen = mockApi();
    renderApp("/monitors/new");
    await screen.findByRole("heading", { name: "Новий монітор" });
    await userEvent.selectOptions(
      screen.getByLabelText("Що перевіряємо"),
      "ssl_expiry",
    );
    expect(screen.queryByLabelText("Дозволені коди")).not.toBeInTheDocument();
    await fill("Назва", "S");
    await fill("URL", "https://s.example.org");
    await fill("Мінімум днів до спливання", "30");
    await userEvent.click(
      screen.getByRole("button", { name: "Створити монітор" }),
    );
    await waitFor(() => expect(seen.posts).toHaveLength(1));
    expect(seen.posts[0]).toMatchObject({
      check_type: "ssl_expiry",
      check_config: { min_days_left: 30 },
    });
  });

  it("puts a server-side config error under the input it is about", async () => {
    mockApi();
    server.use(
      http.post("*/api/v1/monitors", () =>
        HttpResponse.json(
          {
            message: "Перевірте введені дані.",
            errors: {
              check_config: ["keyword: is required, 1-200 characters"],
            },
          },
          { status: 422 },
        ),
      ),
    );
    renderApp("/monitors/new");
    await screen.findByRole("heading", { name: "Новий монітор" });
    await userEvent.selectOptions(
      screen.getByLabelText("Що перевіряємо"),
      "keyword",
    );
    await fill("Назва", "K");
    await fill("URL", "https://k.example.org");
    await userEvent.click(
      screen.getByRole("button", { name: "Створити монітор" }),
    );
    expect(
      await screen.findByText("is required, 1-200 characters"),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Слово")).toHaveAttribute(
      "aria-invalid",
      "true",
    );
  });

  it("editing a keyword monitor shows its saved settings", async () => {
    mockApi();
    server.use(
      http.get("*/api/v1/monitors/7/config", () =>
        HttpResponse.json({
          ...CONFIG,
          check_type: "keyword",
          check_config: {
            keyword: "Hello",
            must_contain: false,
            expected_statuses: [200, "300-399"],
          },
        }),
      ),
    );
    renderApp("/monitors/7/edit");
    await screen.findByRole("heading", { name: "Редагувати монітор" });
    expect(screen.getByLabelText("Що перевіряємо")).toHaveValue("keyword");
    expect(screen.getByLabelText("Слово")).toHaveValue("Hello");
    expect(
      screen.getByLabelText("Слово має бути у відповіді"),
    ).not.toBeChecked();
    expect(screen.getByLabelText("Дозволені коди")).toHaveValue("200,300-399");
  });
});
