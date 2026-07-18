import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";

// Next.js 16 removed the built-in `next lint` wrapper — this is the flat-config replacement,
// using the still-maintained eslint-config-next package directly (see docs/NEXTJS_UPGRADE_PLAN.md).
const eslintConfig = defineConfig([
  ...nextVitals,
  globalIgnores(["node_modules/**", ".next/**", "out/**", "build/**", "next-env.d.ts"]),
]);

export default eslintConfig;
