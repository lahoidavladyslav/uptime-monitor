import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

export type Theme = "light" | "dark" | "system";
const KEY = "uptime-theme";

interface ThemeContext {
  theme: Theme;
  setTheme: (theme: Theme) => void;
}

const Ctx = createContext<ThemeContext | null>(null);

function readStored(): Theme {
  try {
    const v = localStorage.getItem(KEY);
    return v === "light" || v === "dark" || v === "system" ? v : "system";
  } catch {
    return "system"; // storage blocked (private mode, sandboxed iframe)
  }
}

const systemQuery = () => window.matchMedia?.("(prefers-color-scheme: dark)");

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setThemeState] = useState<Theme>(readStored);

  useEffect(() => {
    const mql = systemQuery();
    const apply = (systemDark: boolean) =>
      document.documentElement.classList.toggle("dark", theme === "dark" || (theme === "system" && systemDark));
    apply(!!mql?.matches);
    if (theme !== "system" || !mql) return;
    const onChange = (e: { matches: boolean }) => apply(e.matches);
    mql.addEventListener("change", onChange);
    return () => mql.removeEventListener("change", onChange);
  }, [theme]);

  const setTheme = useCallback((next: Theme) => {
    setThemeState(next);
    try {
      localStorage.setItem(KEY, next);
    } catch {
      /* the choice still applies for this session */
    }
  }, []);

  const value = useMemo(() => ({ theme, setTheme }), [theme, setTheme]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useTheme(): ThemeContext {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useTheme must be used inside <ThemeProvider>");
  return ctx;
}
