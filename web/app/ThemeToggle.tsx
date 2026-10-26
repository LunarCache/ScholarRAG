"use client";

import { useEffect, useState } from "react";

export type ThemeMode = "light" | "dark";

export function useTheme() {
  const [theme, setTheme] = useState<ThemeMode>("light");

  useEffect(() => {
    const saved = localStorage.getItem("scholarrag-theme") as ThemeMode | null;
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const initial: ThemeMode = saved === "dark" || saved === "light" ? saved : (media.matches ? "dark" : "light");
    setTheme(initial);
    document.documentElement.dataset.theme = initial;

    const handleChange = (event: MediaQueryListEvent) => {
      if (!localStorage.getItem("scholarrag-theme")) {
        const next: ThemeMode = event.matches ? "dark" : "light";
        setTheme(next);
        document.documentElement.dataset.theme = next;
      }
    };
    media.addEventListener("change", handleChange);
    return () => media.removeEventListener("change", handleChange);
  }, []);

  function toggleTheme() {
    const next: ThemeMode = theme === "dark" ? "light" : "dark";
    setTheme(next);
    localStorage.setItem("scholarrag-theme", next);
    document.documentElement.dataset.theme = next;
  }

  return { theme, toggleTheme };
}

export function ThemeToggle() {
  const { theme, toggleTheme } = useTheme();
  return (
    <button
      type="button"
      className="themeToggleBtn"
      onClick={toggleTheme}
      title={theme === "dark" ? "切换至浅色模式 (科研白)" : "切换至深色模式 (极夜黑)"}
      aria-label={theme === "dark" ? "切换至浅色模式" : "切换至深色模式"}
    >
      <span aria-hidden="true">{theme === "dark" ? "☀" : "☾"}</span>
      <span className="themeToggleLabel">{theme === "dark" ? "浅色" : "深色"}</span>
    </button>
  );
}
