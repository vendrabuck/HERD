import js from "@eslint/js";
import globals from "globals";
import tseslint from "typescript-eslint";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";

export default tseslint.config(
  { ignores: ["dist"] },
  {
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    files: ["**/*.{ts,tsx}"],
    languageOptions: {
      ecmaVersion: 2020,
      globals: {
        ...globals.browser,
        // vite.config.ts's define block (issue #846); see vite-env.d.ts.
        __APP_VERSION__: "readonly",
        __APP_BUILD__: "readonly",
        __APP_BUILD_DATE__: "readonly",
      },
    },
    plugins: {
      "react-hooks": reactHooks,
      "react-refresh": reactRefresh,
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      "react-refresh/only-export-components": [
        "warn",
        { allowConstantExport: true },
      ],
      "@typescript-eslint/no-unused-vars": [
        "error",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_" },
      ],
    },
  },
  {
    // Issue #1314: error text reaches the UI only through errorDetail (or a
    // structuredDetail narrower) in src/lib/errors.ts. FastAPI's 422 detail is
    // a list of objects, so a cast claiming `detail` is a string, or a raw
    // `.data.detail` read handed to toast.error or JSX, renders an object and
    // throws. errors.ts itself and the tests are exempt.
    files: ["src/**/*.{ts,tsx}"],
    ignores: ["src/lib/errors.ts", "src/test/**"],
    rules: {
      "no-restricted-syntax": [
        "error",
        {
          selector: "TSAsExpression TSPropertySignature[key.name='detail'] TSStringKeyword",
          message:
            "Do not cast an error's detail to string (a 422 detail is a list); use errorDetail from @/lib/errors (issue #1314).",
        },
        {
          selector:
            "TSTypeParameterInstantiation TSPropertySignature[key.name='detail'] TSStringKeyword",
          message:
            "Do not type an error's detail as string (a 422 detail is a list); use errorDetail from @/lib/errors (issue #1314).",
        },
        {
          selector: "TSSatisfiesExpression TSPropertySignature[key.name='detail'] TSStringKeyword",
          message:
            "Do not claim an error's detail is a string (a 422 detail is a list); use errorDetail from @/lib/errors (issue #1314).",
        },
        {
          selector: "MemberExpression[property.name='detail'][object.property.name='data']",
          message:
            "Do not read .data.detail directly; use errorDetail, responseDetail, or a structuredDetail narrower from @/lib/errors (issue #1314).",
        },
        {
          selector:
            "MemberExpression[computed=true][property.value='detail'][object.property.name='data']",
          message:
            "Do not read .data[\"detail\"] directly; use errorDetail, responseDetail, or a structuredDetail narrower from @/lib/errors (issue #1314).",
        },
        {
          // const { detail } = x.data, x?.data, (x as T).data; and the nested
          // const { data: { detail } } = x.response.
          selector:
            ":matches(VariableDeclarator[init.property.name='data'], VariableDeclarator[init.expression.property.name='data'], Property[key.name='data']) > ObjectPattern > Property[key.name='detail']",
          message:
            "Do not destructure detail from a response's data; use errorDetail, responseDetail, or a structuredDetail narrower from @/lib/errors (issue #1314).",
        },
      ],
    },
  }
);
