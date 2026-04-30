import type { Config } from "tailwindcss";

// Tokens declared as CSS custom properties in app/globals.css. Tailwind
// utilities reference them by var() so the design system stays single-source.
const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        ink: {
          0: "var(--ink-0)", 1: "var(--ink-1)", 2: "var(--ink-2)",
          3: "var(--ink-3)", 4: "var(--ink-4)", 5: "var(--ink-5)",
          6: "var(--ink-6)", 7: "var(--ink-7)", 8: "var(--ink-8)",
          9: "var(--ink-9)",
        },
        acc:  "var(--acc)",
        "acc-hi": "var(--acc-hi)",
        "acc-lo": "var(--acc-lo)",
        "acc-bg": "var(--acc-bg)",
        pos:  "var(--pos)",
        neg:  "var(--neg)",
        warn: "var(--warn)",
      },
      fontFamily: {
        // Geist (single-family default — impeccable §Typography "you often
        // don't need a second font"). Fraunces only on the page-hero <h1>.
        sans:  ["Geist", "ui-sans-serif", "system-ui", "sans-serif"],
        serif: ["Fraunces", "ui-serif", "Georgia", "serif"],
        mono:  ["Geist Mono", "ui-monospace", "monospace"],
      },
      fontSize: {
        // 1.25 modular scale; fixed rem (product register).
        xs: "var(--t-xs)", sm: "var(--t-sm)", base: "var(--t-md)",
        lg: "var(--t-lg)", xl: "var(--t-xl)", "2xl": "var(--t-2xl)",
        "3xl": "var(--t-3xl)",
      },
      transitionTimingFunction: {
        "out-quart": "var(--ease-out-quart)",
        "out-expo":  "var(--ease-out-expo)",
      },
      transitionDuration: {
        fast: "var(--d-1)",
        med:  "var(--d-2)",
        slow: "var(--d-3)",
      },
      maxWidth: {
        prose: "65ch",   // typography ref: cap line length 65–75ch
      },
    },
  },
  plugins: [],
};
export default config;
