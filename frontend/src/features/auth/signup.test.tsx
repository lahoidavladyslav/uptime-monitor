import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";

import { server } from "@/test/server";
import { mockSession, renderApp } from "@/test/utils";

async function fillSignup() {
  await userEvent.type(await screen.findByLabelText("Ім'я користувача"), "carol");
  await userEvent.type(screen.getByLabelText("Email"), "carol@example.com");
  await userEvent.type(screen.getByLabelText("Пароль"), "Str0ng-pass-123");
  await userEvent.type(screen.getByLabelText("Підтвердження пароля"), "Str0ng-pass-123");
  await userEvent.click(screen.getByRole("button", { name: "Створити акаунт" }));
}

describe("signup", () => {
  it("shows per-field errors from the API (422)", async () => {
    mockSession();
    server.use(
      http.post("*/api/v1/auth/signup", () =>
        HttpResponse.json(
          { message: "Перевірте введені дані.", errors: { email: ["Користувач з таким email уже існує."], password2: ["Паролі не збігаються."] } },
          { status: 422 },
        ),
      ),
    );
    renderApp("/signup");
    await fillSignup();
    expect(await screen.findByText("Користувач з таким email уже існує.")).toBeInTheDocument();
    expect(screen.getByText("Паролі не збігаються.")).toBeInTheDocument();
    expect(screen.getByLabelText("Email")).toHaveAttribute("aria-invalid", "true");
  });

  it("success goes to /signup/sent and names the address", async () => {
    mockSession();
    server.use(http.post("*/api/v1/auth/signup", () => HttpResponse.json({ status: "confirmation_sent" }, { status: 201 })));
    const { router } = renderApp("/signup");
    await fillSignup();
    await waitFor(() => expect(router.state.location.pathname).toBe("/signup/sent"));
    expect(await screen.findByText(/carol@example\.com/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Не отримали лист?" })).toHaveAttribute("href", "/resend");
  });

  it("opening /signup/sent directly (no state) still renders sensibly", async () => {
    mockSession();
    renderApp("/signup/sent");
    expect(await screen.findByRole("heading", { name: "Перевірте пошту" })).toBeInTheDocument();
  });
});

describe("email confirmation", () => {
  it("a good token confirms and offers to log in", async () => {
    mockSession();
    let body: unknown;
    server.use(
      http.post("*/api/v1/auth/confirm", async ({ request }) => {
        body = await request.json();
        return HttpResponse.json({ status: "confirmed" });
      }),
    );
    renderApp("/confirm/tok123");
    expect(await screen.findByText("Email підтверджено. Тепер можна увійти.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Увійти" })).toHaveAttribute("href", "/login");
    expect(body).toEqual({ token: "tok123" });
  });

  it("a bad token shows the API message and the resend link", async () => {
    mockSession();
    server.use(http.post("*/api/v1/auth/confirm", () => HttpResponse.json({ message: "Посилання недійсне або застаріло.", errors: {} }, { status: 400 })));
    renderApp("/confirm/bad");
    expect(await screen.findByText("Посилання недійсне або застаріло.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Надіслати лист підтвердження ще раз" })).toHaveAttribute("href", "/resend");
  });

  it("confirms exactly once even if the effect re-runs", async () => {
    mockSession();
    let calls = 0;
    server.use(http.post("*/api/v1/auth/confirm", () => ((calls += 1), HttpResponse.json({ status: "confirmed" }))));
    renderApp("/confirm/once");
    await screen.findByText("Email підтверджено. Тепер можна увійти.");
    expect(calls).toBe(1);
  });
});

describe("resend", () => {
  it("shows the same neutral answer", async () => {
    mockSession();
    server.use(http.post("*/api/v1/auth/resend", () => HttpResponse.json({ status: "ok" })));
    renderApp("/resend");
    await userEvent.type(await screen.findByLabelText("Email"), "anyone@example.com");
    await userEvent.click(screen.getByRole("button", { name: "Надіслати" }));
    expect(await screen.findByText("Якщо такий неактивований акаунт існує, ми надіслали лист повторно.")).toBeInTheDocument();
  });
});
