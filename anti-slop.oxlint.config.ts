import landminesRecommended from "@interfere/landmines/configs/recommended.json" with { type: "json" };
import nkzw from "@nkzw/oxlint-config";
import { defineConfig } from "oxlint";

export default defineConfig({
	...nkzw,
	jsPlugins: [
		...nkzw.jsPlugins,
		{
			name: "anti-slop",
			specifier: import.meta.resolve("./anti-slop/index.ts"),
		},
		{
			name: "landmines",
			specifier: import.meta.resolve("@interfere/landmines"),
		},
	],
	options: {
		...nkzw.options,
		reportUnusedDisableDirectives: "off",
	},
	overrides: [
		...nkzw.overrides,
		{
			files: ["**/*.jsx", "**/*.tsx"],
			rules: {
				"react/rules-of-hooks": "error",
			},
		},
	],
	rules: {
		...nkzw.rules,
		"@nkzw/no-instanceof": "off",
		"@typescript-eslint/array-type": ["error", { default: "array" }],
		curly: ["error", "multi-line"],
		"import-x/no-namespace": "off",
		"no-console": "off",
		"oxc/bad-bitwise-operator": "off",
		"react/rules-of-hooks": "off",
		"unicorn/numeric-separators-style": "off",
		"unicorn/prefer-top-level-await": "off",
		"anti-slop/no-module-mocking": "error",
		"anti-slop/no-runtime-typeof": "error",
		"anti-slop/no-shape-in-symbol-names": "error",
		"anti-slop/no-unknown-parameters": "error",
		"anti-slop/require-safety-comment-for-type-assertion": "error",
		...landminesRecommended.rules,
		"landmines/max-files-per-directory": "warn",
		"landmines/no-inline-hex-encoding": "warn",
		"landmines/no-test-doubles": "off",
		"landmines/padding-between-statements": "error",
	},
});
