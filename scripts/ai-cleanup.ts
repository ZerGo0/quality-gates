import { readFile, writeFile } from "node:fs/promises";
import { extname, relative, resolve } from "node:path";

import { type LineRange, readChangedLines, touchesChangedLines } from "./changed-lines.ts";
import { readChangedFiles, readRepositoryFiles, repositoryRoot } from "./repository-files.ts";

const ownPath = relative(repositoryRoot, import.meta.filename);
const checkOnly = process.argv.includes("--check");
const changesOnly = process.argv.includes("--changes");
const scope = changesOnly ? "changed" : "repository";

const binaryExtensions = new Set([
	"png",
	"jpg",
	"jpeg",
	"gif",
	"webp",
	"avif",
	"ico",
	"icns",
	"psd",
	"ttf",
	"otf",
	"woff",
	"woff2",
	"eot",
	"pdf",
	"zip",
	"gz",
	"tar",
	"bz2",
	"xz",
	"7z",
	"rar",
	"mp3",
	"mp4",
	"m4a",
	"mov",
	"avi",
	"webm",
	"wav",
	"flac",
	"wasm",
	"jar",
	"bin",
	"dylib",
	"so",
	"dll",
	"a",
	"o",
	"class",
]);

type Rule = { re: RegExp; to: string };

const rules: Rule[] = [
	{ re: /\p{Cf}/gu, to: "" },
	{ re: /(?![\t\n\r\u0080-\u009F])\p{Cc}/gu, to: "" },
	{ re: /\p{Dash_Punctuation}/gu, to: "-" },
	{ re: /[\u201C\u201D\u201E\u201F\u2033\u301D\u301E\uFF02]/gu, to: '"' },
	{ re: /[\u2018\u2019\u201A\u2032\u02BC\uFF07]/gu, to: "'" },
	{ re: /[\u00A0\u2000-\u200A\u202F\u205F\u3000\u2007]/gu, to: " " },
	{ re: /\u00AD/gu, to: "" },
	{ re: /[\u2039\u00AB\u27E8\u27EA\u276E]/gu, to: "<" },
	{ re: /[\u203A\u00BB\u27E9\u27EB\u276F]/gu, to: ">" },
];

const charMap = new Map(
	Object.entries({
		"×": "x",
		"◦": "·",
		"∙": "·",
		"−": "-",
		"‣": "-",
		"‽": "?!",
		"→": "->",
		"⟶": "->",
		"➝": "->",
		"➔": "->",
		"➜": "->",
		"➙": "->",
		"➡": "->",
		"↦": "->",
		"↠": "->",
		"⇢": "->",
		"←": "<-",
		"⟵": "<-",
		"⬅": "<-",
		"↤": "<-",
		"↞": "<-",
		"↖": "<-",
		"↙": "<-",
		"↗": "->",
		"↘": "->",
		"↔": "<->",
		"⟷": "<->",
		"⇔": "<->",
		"⇒": "=>",
		"⇐": "<=",
		"⟹": "=>",
		"⟸": "<=",
		"⟺": "<=>",
		"≤": "<=",
		"≥": ">=",
		"≠": "!=",
		"≈": "~=",
		"≅": "~=",
		"≡": "===",
		"±": "+/-",
		"∓": "-/+",
		"÷": "/",
		"∕": "/",
		"⁄": "/",
		"∗": "*",
	}),
);

const charClassSpecials = /[\\^\-\][]/gu;

function escapeForCharClass(value: string): string {
	return value.replaceAll(charClassSpecials, String.raw`\$&`);
}

const charMapPattern = new RegExp(
	`[${[...charMap.keys()].map(escapeForCharClass).join("")}]`,
	"g",
);

function normalizeNfkc(text: string): string {
	return text.replaceAll(/[^…]+/gu, (segment) => segment.normalize("NFKC"));
}

function applyRules(text: string) {
	let out = normalizeNfkc(text);
	let replacements = out === text ? 0 : 1;

	for (const { re, to } of rules) {
		out = out.replace(re, (match) => {
			if (match !== to) replacements++;

			return to;
		});
	}

	out = out.replaceAll(charMapPattern, (match) => {
		replacements++;

		return charMap.get(match) ?? match;
	});

	return { out, replacements };
}

function cleanText(text: string, ranges: LineRange[] | undefined) {
	if (ranges === undefined) return applyRules(text);

	let replacements = 0;

	const out = text
		.split("\n")
		.map((line, index) => {
			if (!touchesChangedLines(ranges, index + 1, index + 1, 0)) return line;

			const cleaned = applyRules(line);

			replacements += cleaned.replacements;

			return cleaned.out;
		})
		.join("\n");

	return { out, replacements };
}

function ignoreMissingFile(error: NodeJS.ErrnoException): undefined {
	if (error.code === "ENOENT") return undefined;

	throw error;
}

async function readTextFile(path: string): Promise<string | undefined> {
	if (binaryExtensions.has(extname(path).toLowerCase().slice(1))) return undefined;

	const buffer = await readFile(path).catch(ignoreMissingFile);

	if (buffer === undefined || buffer.includes(0)) return undefined;

	return buffer.toString("utf8");
}

async function cleanFile(file: string, ranges: LineRange[] | undefined): Promise<number> {
	const path = resolve(repositoryRoot, file);

	const text = await readTextFile(path);

	if (text === undefined) return 0;

	const { out, replacements } = cleanText(text, ranges);

	if (out === text) return 0;

	if (!checkOnly) await writeFile(path, out, "utf8");

	return replacements;
}

const files = (changesOnly ? readChangedFiles() : readRepositoryFiles()).filter((file) => file !== ownPath);

if (files.length === 0) {
	console.log(`No ${scope} files for ai-cleanup`);

	process.exit(0);
}

const changedLines = changesOnly ? readChangedLines(files) : new Map<string, LineRange[]>();
const fixCommand = changesOnly ? "quality-gates cleanup --changes" : "quality-gates cleanup";

let changedFiles = 0;

for (const file of files) {
	const replacements = await cleanFile(file, changedLines.get(file));

	if (replacements === 0) continue;

	changedFiles++;

	console.log(
		checkOnly
			? `error: ${file}: ${replacements} AI typography replacement(s) needed; run ${fixCommand}`
			: `changed: ${file} (${replacements})`,
	);
}

console.log(
	`ai-cleanup ${checkOnly ? "found" : "fixed"} ${changedFiles} of ${files.length} ${scope} file(s)`,
);

if (checkOnly && changedFiles > 0) process.exitCode = 1;
