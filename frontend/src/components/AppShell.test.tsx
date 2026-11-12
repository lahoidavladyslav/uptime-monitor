import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";

import { t } from "@/lib/strings";

import { AppShell } from "./AppShell";
import { ThemeProvider } from "./ThemeProvider";

function setup(onLogout = vi.fn()) {
  window.matchMedia = vi.fn().mockReturnValue({ matches: false, addEventListener() {}, removeEventListener() {} }) as never;
  render(
    <ThemeProvider>
      <MemoryRouter>
        <AppShell username="alice" onLogout={onLogout}>
          <p>content</p>
        </AppShell>
      </MemoryRouter>
    </ThemeProvider>,
  );
  return onLogout;
}

describe("AppShell", () => {
  it("renders the desktop sidebar and the mobile bottom bar, each with the four sections", () => {
    setup();
    const side = screen.getByRole("complementary");
    const bottom = screen.getByRole("navigation", { name: t.nav.bottom });
    for (const nav of [side, bottom]) {
      for (const label of [t.nav.monitors, t.nav.statusPages, t.nav.reports, t.nav.security]) {
        expect(within(nav).getByText(label)).toBeInTheDocument();
      }
    }
    expect(screen.getByText("content")).toBeInTheDocument();
  });

  it("shows the user name and logs out", async () => {
    const onLogout = setup();
    expect(screen.getByText("alice")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: t.nav.logout }));
    expect(onLogout).toHaveBeenCalledTimes(1);
  });

  it("has a theme switch with the three choices", async () => {
    setup();
    await userEvent.click(screen.getByRole("button", { name: t.nav.theme }));
    for (const label of [t.nav.themeLight, t.nav.themeDark, t.nav.themeSystem]) {
      expect(screen.getByRole("menuitemradio", { name: label })).toBeInTheDocument();
    }
  });
});
