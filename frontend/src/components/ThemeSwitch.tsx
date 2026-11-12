import { Check, Monitor, Moon, Sun } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { t } from "@/lib/strings";

import { useTheme, type Theme } from "./ThemeProvider";
import { Button } from "./ui/button";

const OPTIONS: { value: Theme; label: string; icon: typeof Sun }[] = [
  { value: "light", label: t.nav.themeLight, icon: Sun },
  { value: "dark", label: t.nav.themeDark, icon: Moon },
  { value: "system", label: t.nav.themeSystem, icon: Monitor },
];

export function ThemeSwitch() {
  const { theme, setTheme } = useTheme();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    const onClick = (e: MouseEvent) => ref.current && !ref.current.contains(e.target as Node) && setOpen(false);
    document.addEventListener("keydown", onKey);
    document.addEventListener("mousedown", onClick);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("mousedown", onClick);
    };
  }, [open]);

  const Current = OPTIONS.find((o) => o.value === theme)?.icon ?? Monitor;
  return (
    <div ref={ref} className="relative">
      <Button variant="ghost" size="icon" aria-label={t.nav.theme} aria-haspopup="menu" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
        <Current aria-hidden="true" className="size-4" />
      </Button>
      {open && (
        <div role="menu" className="absolute right-0 z-20 mt-1 w-40 rounded-lg border border-border bg-card p-1">
          {OPTIONS.map(({ value, label, icon: Icon }) => (
            <button
              key={value}
              role="menuitemradio"
              aria-checked={theme === value}
              className="flex w-full cursor-pointer items-center gap-2 rounded-md px-2 py-1.5 text-sm hover:bg-muted"
              onClick={() => {
                setTheme(value);
                setOpen(false);
              }}
            >
              <Icon aria-hidden="true" className="size-4" />
              {label}
              {theme === value && <Check aria-hidden="true" className="ml-auto size-4" />}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
