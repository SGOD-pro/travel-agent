import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";

const eslintConfig = defineConfig([
  ...nextVitals,
  ...nextTs,
  globalIgnores([
    // Default ignores of eslint-config-next:
    ".next/**",
    "out/**",
    "build/**",
    "next-env.d.ts",
    // Playwright artifacts:
    "test-results/**",
    "playwright-report/**",
    "blob-report/**",
    "playwright/.cache/**",
  ]),
  {
    rules: {
      // Allow the conventional underscore prefix for intentionally
      // unused arguments and destructured variables.
      "@typescript-eslint/no-unused-vars": [
        "warn",
        {
          argsIgnorePattern: "^_",
          varsIgnorePattern: "^_",
          caughtErrorsIgnorePattern: "^_",
        },
      ],
      // Pre-existing interaction patterns (setState inside effects, recursive
      // poll scheduling) predate the react-hooks v6 static checks. Refactoring
      // them is behavior-sensitive work tracked under spec 0007 follow-ups;
      // keep them visible as warnings until then.
      "react-hooks/set-state-in-effect": "warn",
      "react-hooks/immutability": "warn",
    },
  },
  {
    // Wire-format type definitions mirror dynamic JSON API payloads.
    files: ["src/types/**/*.ts"],
    rules: {
      "@typescript-eslint/no-explicit-any": "off",
    },
  },
  {
    // Playwright fixtures intentionally stay loosely typed.
    files: ["e2e/**/*.ts"],
    rules: {
      "@typescript-eslint/no-explicit-any": "off",
    },
  },
]);

export default eslintConfig;
