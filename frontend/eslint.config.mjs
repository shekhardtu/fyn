import eslint from "@eslint/js";
import { defineConfig, globalIgnores } from "eslint/config";
import { reactRefresh } from "eslint-plugin-react-refresh";
import reactHooks from "eslint-plugin-react-hooks";
import globals from "globals";
import tseslint from "typescript-eslint";

export default defineConfig(
  globalIgnores(["dist/**", "coverage/**", "playwright-report/**", "test-results/**"]),
  {
    files: ["**/*.{ts,tsx}"],
    extends: [eslint.configs.recommended, ...tseslint.configs.recommended],
    languageOptions: {
      ecmaVersion: "latest",
      globals: { ...globals.browser, ...globals.node },
    },
  },
  reactHooks.configs.flat.recommended,
  reactRefresh.configs.vite(),
  {
    rules: {
      // Existing component modules intentionally co-locate small constants and
      // hooks. Route modules stay strict below; mixed feature modules may cause
      // a full HMR reload but do not create a production correctness issue.
      "react-refresh/only-export-components": "off",
    },
  },
  {
    files: ["src/routes/**/*.tsx"],
    rules: { "react-refresh/only-export-components": "error" },
  },
);
