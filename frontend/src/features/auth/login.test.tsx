import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { delay, http, HttpResponse } from "msw";

import { server } from "@/test/server";
import { mockSession, renderApp } from "@/test/utils";

const OK = () => HttpResponse.json({ status: "ok" });
const unprocessable = (errors: Record<string, string[]>, message = "Перевірте введені дані.") =>
  HttpResponse.json({ message, errors }, { status: 422 });

async function fillLogin(username = "alice", password = "pw12345678") {
  await userEvent.type(await screen.findByLabelText("Ім'я користувача"), username);
  await userEvent.type(screen.getByLabelText("Пароль"), password);
  await userEvent.click(screen.getByRole("button", { name: "Увійти" }));
}

function loginOk(session: { authed: boolean }) {
  server.use(
    http.post("*/api/v1/auth/login", () => {
      session.authed = true;
      return OK();
    }),
  );
}

describe("login", () => {
  it("signs in and lands on / (inside the app shell)", async () => {
    const session = mockSession();
    loginOk(session);
    const { router } = renderApp("/login");
    await fillLogin();
    await waitFor(() => expect(router.state.location.pathname).toBe("/"));
    expect(await screen.findByText("alice")).toBeInTheDocument();
  });

  it("honours a same-origin ?next=", async () => {
    const session = mockSession();
    loginOk(session);
    const { router } = renderApp("/login?next=%2Fmonitors%2F5");
    await fillLogin();
    await waitFor(() => expect(router.state.location.pathname).toBe("/monitors/5"));
  });

  it.each(["https%3A%2F%2Fevil.example%2F", "%2F%2Fevil.example", "javascript%3Aalert(1)", "%2F%5Cevil.example"])(
    "ignores an off-site next=%s",
    async (next) => {
      const session = mockSession();
      loginOk(session);
      const { router } = renderApp(`/login?next=${next}`);
      await fillLogin();
      await waitFor(() => expect(router.state.location.pathname).toBe("/"));
    },
  );

  it("shows the API's form-level error and re-enables the button", async () => {
    mockSession();
    server.use(http.post("*/api/v1/auth/login", () => unprocessable({ __all__: ["Невірний логін чи пароль."] })));
    renderApp("/login");
    await fillLogin("alice", "wrong");
    expect(await screen.findByRole("alert")).toHaveTextContent("Невірний логін чи пароль.");
    expect(screen.getByRole("button", { name: "Увійти" })).toBeEnabled();
  });

  it("does not allow a double submit while the request is pending", async () => {
    const session = mockSession();
    let calls = 0;
    server.use(
      http.post("*/api/v1/auth/login", async () => {
        calls += 1;
        await delay(80);
        session.authed = true;
        return OK();
      }),
    );
    renderApp("/login");
    await fillLogin();
    expect(screen.getByRole("button", { name: /Увійти/ })).toBeDisabled();
    await userEvent.keyboard("{Enter}");
    await waitFor(() => expect(screen.queryByLabelText("Ім'я користувача")).not.toBeInTheDocument());
    expect(calls).toBe(1);
  });

  it("empty fields are caught client-side (shape only) without a request", async () => {
    mockSession();
    let calls = 0;
    server.use(http.post("*/api/v1/auth/login", () => ((calls += 1), OK())));
    renderApp("/login");
    await userEvent.click(await screen.findByRole("button", { name: "Увійти" }));
    expect((await screen.findAllByText("Обов'язкове поле")).length).toBeGreaterThan(0);
    expect(calls).toBe(0);
  });
});

describe("login with 2FA", () => {
  function twoFactor(session: { authed: boolean }, verify: () => Response) {
    server.use(
      http.post("*/api/v1/auth/login", () => HttpResponse.json({ status: "2fa_required" })),
      http.post("*/api/v1/auth/login/2fa", () => {
        const r = verify();
        if (r.status === 200) session.authed = true;
        return r;
      }),
    );
  }

  it("swaps to the 2FA form and completes the login", async () => {
    const session = mockSession();
    twoFactor(session, () => HttpResponse.json({ status: "ok" }));
    const { router } = renderApp("/login?next=%2Fmonitors%2F9");
    await fillLogin();
    await userEvent.type(await screen.findByLabelText("Код"), "123456");
    await userEvent.click(screen.getByRole("button", { name: "Підтвердити" }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/monitors/9"));
  });

  it("shows the API message under the code field on a wrong code (422)", async () => {
    const session = mockSession();
    twoFactor(session, () => unprocessable({ code: ["Невірний код."] }));
    renderApp("/login");
    await fillLogin();
    await userEvent.type(await screen.findByLabelText("Код"), "000000");
    await userEvent.click(screen.getByRole("button", { name: "Підтвердити" }));
    expect(await screen.findByText("Невірний код.")).toBeInTheDocument();
    expect(screen.getByLabelText("Код")).toBeInTheDocument(); // still on the 2FA step
  });

  it("an expired challenge (401) returns to the password form with a notice", async () => {
    const session = mockSession();
    twoFactor(session, () => HttpResponse.json({ message: "Сесія входу завершилась. Увійдіть знову.", errors: {} }, { status: 401 }));
    renderApp("/login");
    await fillLogin();
    await userEvent.type(await screen.findByLabelText("Код"), "123456");
    await userEvent.click(screen.getByRole("button", { name: "Підтвердити" }));
    expect(await screen.findByLabelText("Ім'я користувача")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Сесія входу завершилась. Увійдіть знову.");
  });

  it("can switch to a recovery code", async () => {
    const session = mockSession();
    twoFactor(session, () => HttpResponse.json({ status: "ok" }));
    renderApp("/login");
    await fillLogin();
    await userEvent.click(await screen.findByRole("button", { name: "Використати код відновлення" }));
    expect(screen.getByText("Введіть один із кодів відновлення.")).toBeInTheDocument();
  });
});
