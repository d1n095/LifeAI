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
    },
  },
  plugins: [],
};

export default config;
