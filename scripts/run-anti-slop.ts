import { spawnSync } from "node:child_process";
import { statSync } from "node:fs";
import { extname, resolve } from "node:path";

import { z } from "zod";

import { type LineRange, readChangedLines, touchesChangedLines } from "./changed-lines.ts";
import {
	qualityGatesRoot,
	readChangedFiles,
	readRepositoryFiles,
	repositoryRoot,
} from "./repository-files.ts";

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

const linesAboveFinding = 1;
const maxReportBytes = 512 * 1024 * 1024;

const spanSchema = z.object({
	column: z.number(),
	length: z.number(),
	line: z.number(),
	offset: z.number(),
});

const reportSchema = z.object({
	diagnostics: z.array(
		z.object({
			code: z.string().optional(),
			filename: z.string(),
			help: z.string().optional(),
			labels: z.array(z.object({ span: spanSchema })),
			message: z.string(),
			severity: z.string(),
		}),
	),
});

type Span = z.infer<typeof spanSchema>;

type Diagnostic = z.infer<typeof reportSchema>["diagnostics"][number];

const fullRepository = process.argv.includes("--all");
const scope = fullRepository ? "in the repository" : "on changed lines";

const lintableFiles = (fullRepository ? readRepositoryFiles() : readChangedFiles()).filter((file) =>
	supportedExtensions.has(extname(file)),
);

if (lintableFiles.length === 0) {
	process.stdout.write(`No JavaScript or TypeScript files ${scope} for anti-slop\n`);

	process.exit(0);
}

const lintResult = spawnSync(
	resolve(qualityGatesRoot, "node_modules", ".bin", "oxlint"),
	[
		"--config",
		resolve(qualityGatesRoot, "anti-slop.oxlint.config.ts"),
		"--format",
		"json",
		"--",
		...lintableFiles,
	],
	{
		cwd: repositoryRoot,
		encoding: "utf8",
		env: {
			...process.env,
			OXLINT_TSGOLINT_PATH: resolve(qualityGatesRoot, "node_modules", ".bin", "tsrs-lint"),
		},
		maxBuffer: maxReportBytes,
	},
);

if (lintResult.error !== undefined) {
	throw lintResult.error;
}

if (lintResult.status !== 0 && lintResult.status !== 1) {
	process.stdout.write(lintResult.stdout);

	process.stderr.write(lintResult.stderr);

	process.exit(lintResult.status ?? 1);
}

const { diagnostics } = reportSchema.parse(JSON.parse(lintResult.stdout));

const changedLines = fullRepository
	? new Map<string, LineRange[]>()
	: readChangedLines(lintableFiles);

const fileSizes = new Map<string, number>();

function diagnosticLine(diagnostic: Diagnostic): number {
	return diagnostic.labels[0]?.span.line ?? 0;
}

function severityRank(diagnostic: Diagnostic): number {
	return Number(diagnostic.severity === "error");
}

function relevantEndLine(file: string, span: Span): number {
	const size = fileSizes.get(file) ?? statSync(resolve(repositoryRoot, file)).size;

	fileSizes.set(file, size);

	return span.offset === 0 && span.length >= size ? Number.MAX_SAFE_INTEGER : span.line;
}

const reported = diagnostics
	.filter((diagnostic) => {
		const span = diagnostic.labels[0]?.span;

		return (
			span === undefined ||
			touchesChangedLines(
				changedLines.get(diagnostic.filename),
				span.line,
				relevantEndLine(diagnostic.filename, span),
				linesAboveFinding,
			)
		);
	})
	.toSorted(
		(left, right) =>
			severityRank(right) - severityRank(left) ||
			left.filename.localeCompare(right.filename) ||
			diagnosticLine(left) - diagnosticLine(right),
	);

for (const diagnostic of reported) {
	const span = diagnostic.labels[0]?.span;
	const location = span === undefined ? "" : `:${span.line}:${span.column}`;
	const help = diagnostic.help === undefined ? "" : ` help: ${diagnostic.help}`;

	process.stdout.write(
		`${diagnostic.filename}${location}: ${diagnostic.severity} ${diagnostic.code ?? "oxlint"}: ${diagnostic.message}${help}\n`,
	);
}

const errors = reported.filter((diagnostic) => diagnostic.severity === "error").length;
const warnings = reported.length - errors;
const hidden = diagnostics.length - reported.length;

process.stdout.write(
	`anti-slop: ${errors} errors, ${warnings} warnings ${scope}; ${hidden} findings on unchanged lines not reported\n`,
);

process.exit(errors > 0 ? 1 : 0);
