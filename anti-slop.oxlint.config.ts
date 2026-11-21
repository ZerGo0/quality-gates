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
	],
	rules: {
		...nkzw.rules,
		"anti-slop/no-module-mocking": "error",
		"anti-slop/no-runtime-typeof": "error",
		"anti-slop/no-shape-in-symbol-names": "error",
		"anti-slop/no-unknown-parameters": "error",
		"anti-slop/require-safety-comment-for-type-assertion": "error",
	},
});
