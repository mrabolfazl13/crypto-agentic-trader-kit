/** Tailwind tuned to the hand-written design system: utilities only, no preflight. */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  corePlugins: { preflight: false },
  theme: {
    extend: {
      colors: {
        bg: "var(--bg)",
        elev: "var(--bg-elev)",
        surface: "var(--surface)",
        surface2: "var(--surface-2)",
        surface3: "var(--surface-3)",
        line: "var(--border)",
        "line-strong": "var(--border-strong)",
        ink: "var(--text)",
        dim: "var(--text-dim)",
        mute: "var(--text-mute)",
        pos: "var(--pos)",
        neg: "var(--neg)",
        warn: "var(--warn)",
        danger: "var(--danger)",
        info: "var(--info)",
        accent: "var(--accent)",
      },
      fontFamily: {
        sans: ["Vazirmatn", "Noto Naskh Arabic", "Segoe UI", "Tahoma", "sans-serif"],
        mono: ["JetBrains Mono", "Cascadia Mono", "Consolas", "monospace"],
      },
      borderRadius: {
        xl: "var(--radius-3)",
        lg: "var(--radius-2)",
        md: "var(--radius-1)",
      },
      boxShadow: {
        soft: "var(--shadow)",
      },
    },
  },
  plugins: [],
};
