import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ThemeProvider, useTheme } from "./ThemeProvider";

type Listener = (e: { matches: boolean }) => void;

function mockSystem(dark: boolean) {
  let listener: Listener | null = null;
  const mql = {
    matches: dark,
    addEventListener: (_: string, l: Listener) => (listener = l),
    removeEventListener: () => (listener = null),
  };
  window.matchMedia = vi.fn().mockReturnValue(mql) as unknown as typeof window.matchMedia;
  return {
    flip(next: boolean) {
      mql.matches = next;
      act(() => listener?.({ matches: next }));
    },
  };
}

function Probe() {
  const { theme, setTheme } = useTheme();
  return (
    <>
      <span data-testid="theme">{theme}</span>
      <button onClick={() => setTheme("dark")}>dark</button>
      <button onClick={() => setTheme("light")}>light</button>
      <button onClick={() => setTheme("system")}>system</button>
    </>
  );
}

const renderProbe = () =>
  render(
    <ThemeProvider>
      <Probe />
    </ThemeProvider>,
  );

beforeEach(() => {
  localStorage.clear();
  document.documentElement.classList.remove("dark");
});

describe("ThemeProvider", () => {
  it("defaults to system and stays light when the system is light", () => {
    mockSystem(false);
    renderProbe();
    expect(screen.getByTestId("theme")).toHaveTextContent("system");
    expect(document.documentElement).not.toHaveClass("dark");
  });

  it("system + dark OS => dark class, and it follows live OS changes", () => {
    const sys = mockSystem(true);
    renderProbe();
    expect(document.documentElement).toHaveClass("dark");
    sys.flip(false);
    expect(document.documentElement).not.toHaveClass("dark");
  });

  it("explicit choices set/remove the dark class and persist under uptime-theme", async () => {
    mockSystem(false);
    renderProbe();
    await userEvent.click(screen.getByText("dark"));
    expect(document.documentElement).toHaveClass("dark");
    expect(localStorage.getItem("uptime-theme")).toBe("dark");
    await userEvent.click(screen.getByText("light"));
    expect(document.documentElement).not.toHaveClass("dark");
    expect(localStorage.getItem("uptime-theme")).toBe("light");
  });

  it("restores a stored choice on start", () => {
    mockSystem(false);
    localStorage.setItem("uptime-theme", "dark");
    renderProbe();
    expect(screen.getByTestId("theme")).toHaveTextContent("dark");
    expect(document.documentElement).toHaveClass("dark");
  });

  it("ignores garbage in storage and survives unavailable storage", () => {
    mockSystem(false);
    localStorage.setItem("uptime-theme", "neon");
    renderProbe();
    expect(screen.getByTestId("theme")).toHaveTextContent("system");
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    expect(() => renderProbe()).not.toThrow();
    vi.restoreAllMocks();
  });
});
