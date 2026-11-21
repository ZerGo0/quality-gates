import { spawnSync } from "node:child_process";
import { extname, resolve } from "node:path";

import { qualityGatesRoot, readChangedFiles, repositoryRoot } from "./repository-files.ts";

const supportedExtensions = new Set([
	".cjs",
	".cts",
	".js",
	".jsx",
	".mjs",
	".mts",
	".ts",
	".tsx",
]);

const lintableFiles = readChangedFiles().filter((file) => supportedExtensions.has(extname(file)));

if (lintableFiles.length === 0) {
	process.stdout.write("No changed JavaScript or TypeScript files for anti-slop\n");
	process.exit(0);
}

const lintResult = spawnSync(
	resolve(qualityGatesRoot, "node_modules", ".bin", "oxlint"),
	["--config", resolve(qualityGatesRoot, "anti-slop.oxlint.config.ts"), "--", ...lintableFiles],
	{
		cwd: repositoryRoot,
		env: {
			...process.env,
			OXLINT_TSGOLINT_PATH: resolve(qualityGatesRoot, "node_modules", ".bin", "tsrs-lint"),
		},
		stdio: "inherit",
	},
);

if (lintResult.error !== undefined) {
	throw lintResult.error;
}
process.exit(lintResult.status ?? 1);
