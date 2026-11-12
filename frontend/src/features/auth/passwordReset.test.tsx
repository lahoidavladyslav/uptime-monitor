import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";

import { server } from "@/test/server";
import { mockSession, renderApp } from "@/test/utils";

const NEUTRAL = "Якщо акаунт із таким email існує, ми надіслали інструкцію зі скидання пароля.";

describe("forgot password", () => {
  it("shows the same neutral confirmation whatever the email", async () => {
    mockSession();
    const seen: unknown[] = [];
    server.use(
      http.post("*/api/v1/auth/password-reset", async ({ request }) => {
        seen.push(await request.json());
        return HttpResponse.json({ status: "ok" });
      }),
    );
    renderApp("/forgot-password");
    await userEvent.type(await screen.findByLabelText("Email"), "nobody@example.com");
    await userEvent.click(screen.getByRole("button", { name: "Надіслати посилання" }));
    expect(await screen.findByText(NEUTRAL)).toBeInTheDocument();
    expect(seen).toEqual([{ email: "nobody@example.com" }]);
  });

  it("shows a format error from the API under the field", async () => {
    mockSession();
    server.use(
      http.post("*/api/v1/auth/password-reset", () =>
        HttpResponse.json({ message: "Перевірте введені дані.", errors: { email: ["Введіть коректну адресу електронної пошти."] } }, { status: 422 }),
      ),
    );
    renderApp("/forgot-password");
    await userEvent.type(await screen.findByLabelText("Email"), "not-an-email");
    await userEvent.click(screen.getByRole("button", { name: "Надіслати посилання" }));
    expect(await screen.findByText("Введіть коректну адресу електронної пошти.")).toBeInTheDocument();
    expect(screen.queryByText(NEUTRAL)).not.toBeInTheDocument();
  });
});

describe("reset password", () => {
  async function fill(p1 = "Another-pass-987", p2 = "Another-pass-987") {
    await userEvent.type(await screen.findByLabelText("Новий пароль"), p1);
    await userEvent.type(screen.getByLabelText("Підтвердження нового пароля"), p2);
    await userEvent.click(screen.getByRole("button", { name: "Зберегти пароль" }));
  }

  it("success sends uid+token from the URL and redirects to /login with a notice", async () => {
    mockSession();
    let body: unknown;
    server.use(
      http.post("*/api/v1/auth/password-reset/confirm", async ({ request }) => {
        body = await request.json();
        return HttpResponse.json({ status: "ok" });
      }),
    );
    const { router } = renderApp("/reset-password/MQ/abc-123");
    await fill();
    await waitFor(() => expect(router.state.location.pathname).toBe("/login"));
    expect(await screen.findByRole("status")).toHaveTextContent("Пароль змінено. Тепер можна увійти.");
    expect(body).toEqual({ uid: "MQ", token: "abc-123", new_password1: "Another-pass-987", new_password2: "Another-pass-987" });
  });

  it("weak password: API field errors appear under the fields", async () => {
    mockSession();
    server.use(
      http.post("*/api/v1/auth/password-reset/confirm", () =>
        HttpResponse.json(
          { message: "Перевірте введені дані.", errors: { new_password2: ["Цей пароль занадто короткий."] } },
          { status: 422 },
        ),
      ),
    );
    renderApp("/reset-password/MQ/abc");
    await fill("123", "123");
    expect(await screen.findByText("Цей пароль занадто короткий.")).toBeInTheDocument();
    expect(screen.getByLabelText("Підтвердження нового пароля")).toHaveAttribute("aria-invalid", "true");
  });

  it("an invalid link (400) shows the message and a way to request a new one", async () => {
    mockSession();
    server.use(
      http.post("*/api/v1/auth/password-reset/confirm", () =>
        HttpResponse.json({ message: "Посилання недійсне або застаріло.", errors: {} }, { status: 400 }),
      ),
    );
    renderApp("/reset-password/MQ/expired");
    await fill();
    expect(await screen.findByText("Посилання недійсне або застаріло.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Запросити нове посилання" })).toHaveAttribute("href", "/forgot-password");
    expect(screen.queryByLabelText("Новий пароль")).not.toBeInTheDocument();
  });
});
