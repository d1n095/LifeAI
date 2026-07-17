import type { Config } from "tailwindcss";

const config: Config = {
  darkMode: "class",
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        base: "#05050a",
        panel: "#0d0d16",
        border: "#1c1c2a",
        accent: "#7c5cff",
        accent2: "#3ce0ff",
      },
      keyframes: {
        "orb-pulse": {
          "0%, 100%": { transform: "scale(1)", opacity: "0.92" },
          "50%": { transform: "scale(1.06)", opacity: "1" },
        },
        "orb-spin": {
          "0%": { transform: "rotate(0deg) scale(1)" },
          "50%": { transform: "rotate(180deg) scale(1.03)" },
          "100%": { transform: "rotate(360deg) scale(1)" },
        },
        "orb-flash": {
          "0%, 100%": { opacity: "1" },
          "50%": { opacity: "0.35" },
        },
      },
      animation: {
        "orb-idle": "orb-pulse 4s ease-in-out infinite",
        "orb-listening": "orb-pulse 1s ease-in-out infinite",
        "orb-thinking": "orb-spin 2.4s linear infinite",
        "orb-speaking": "orb-pulse 0.55s ease-in-out infinite",
        "orb-error": "orb-flash 0.5s ease-in-out 3",
      },
    },
  },
  plugins: [],
};

export default config;
