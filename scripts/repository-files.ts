import { spawnSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { matchesGlob, resolve } from "node:path";
import { fileURLToPath } from "node:url";

export const qualityGatesRoot = fileURLToPath(new URL("..", import.meta.url));

function runGit(arguments_: string[], cwd: string): string {
	const result = spawnSync("git", arguments_, {
		cwd,
		encoding: "utf8",
	});

	if (result.error !== undefined) throw result.error;
	if (result.status !== 0) {
		process.stderr.write(result.stderr);
		throw new Error(`git ${arguments_.join(" ")} failed with status ${result.status}`);
	}

	return result.stdout;
}

export const repositoryRoot = runGit(["rev-parse", "--show-toplevel"], process.cwd()).trim();

function readGitPaths(arguments_: string[]): string[] {
	return runGit(arguments_, repositoryRoot).split("\0").filter(Boolean);
}

function readIgnorePatterns(): string[] {
	const ignoreFile = resolve(repositoryRoot, ".aislopignore");
	if (!existsSync(ignoreFile)) return [];
	return readFileSync(ignoreFile, "utf8")
		.split(/\r?\n/)
		.map((line) => line.trim())
		.filter((line) => line.length > 0 && !line.startsWith("#"));
}

function isIgnored(file: string, patterns: string[]): boolean {
	return patterns.some((pattern) =>
		matchesGlob(file, pattern.endsWith("/") ? `${pattern}**` : pattern),
	);
}

function withoutIgnored(files: Iterable<string>): string[] {
	const ignorePatterns = readIgnorePatterns();
	return [...files].filter((file) => !isIgnored(file, ignorePatterns));
}

export function readRepositoryFiles(): string[] {
	return withoutIgnored(
		readGitPaths(["ls-files", "--cached", "--others", "--exclude-standard", "-z", "--"]),
	);
}

export function readChangedFiles(): string[] {
	return withoutIgnored(
		new Set([
			...readGitPaths(["diff", "--name-only", "--diff-filter=ACMR", "-z", "HEAD", "--"]),
			...readGitPaths(["ls-files", "--others", "--exclude-standard", "-z", "--"]),
		]),
	);
}
