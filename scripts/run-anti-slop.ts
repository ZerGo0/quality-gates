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
	console.log("No changed JavaScript or TypeScript files for anti-slop");
	process.exit(0);
}

const lintResult = spawnSync(
	resolve(qualityGatesRoot, "node_modules", ".bin", "oxlint"),
	["--config", resolve(qualityGatesRoot, "anti-slop.oxlintrc.json"), "--", ...lintableFiles],
	{
		cwd: repositoryRoot,
		stdio: "inherit",
	},
);

if (lintResult.error !== undefined) throw lintResult.error;
process.exit(lintResult.status ?? 1);
