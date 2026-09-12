/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      colors: {
        ink: {
          950: "#0b1220",
          900: "#111827",
          800: "#1f2937",
          700: "#334155",
        },
        tide: {
          400: "#2dd4bf",
          500: "#14b8a6",
          600: "#0d9488",
        },
        ember: {
          400: "#fb923c",
          500: "#f97316",
        },
      },
      fontFamily: {
        display: ['"Fraunces"', "Georgia", "serif"],
        sans: ['"DM Sans"', "system-ui", "sans-serif"],
        mono: ['"IBM Plex Mono"', "ui-monospace", "monospace"],
      },
      boxShadow: {
        panel: "0 20px 50px rgba(11, 18, 32, 0.35)",
      },
      keyframes: {
        rise: {
          "0%": { opacity: "0", transform: "translateY(12px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
        pulseSoft: {
          "0%, 100%": { opacity: "0.55" },
          "50%": { opacity: "1" },
        },
        scan: {
          "0%": { transform: "translateX(-30%)" },
          "100%": { transform: "translateX(130%)" },
        },
      },
      animation: {
        rise: "rise 0.55s ease-out both",
        pulseSoft: "pulseSoft 2.4s ease-in-out infinite",
        scan: "scan 2.8s linear infinite",
      },
    },
  },
  plugins: [],
};
