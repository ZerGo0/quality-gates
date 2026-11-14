import { readFile, writeFile } from "node:fs/promises";
import { extname, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { readChangedFiles, readRepositoryFiles, repositoryRoot } from "./repository-files.ts";

const ownPath = relative(repositoryRoot, fileURLToPath(import.meta.url));
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
	{ re: /[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F]/g, to: "" },
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

const charClassSpecials = new Set(["\\", "^", "-", "]", "["]);

function escapeForCharClass(value: string): string {
	return [...value].map((char) => (charClassSpecials.has(char) ? `\\${char}` : char)).join("");
}

const charMapPattern = new RegExp(
	`[${[...charMap.keys()].map(escapeForCharClass).join("")}]`,
	"g",
);

function normalizeNfkc(text: string): string {
	return text.replace(/[^…]+/gu, (segment) => segment.normalize("NFKC"));
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
	out = out.replace(charMapPattern, (match) => {
		replacements++;
		return charMap.get(match) ?? match;
	});
	return { out, replacements };
}

async function cleanFile(file: string): Promise<number> {
	if (binaryExtensions.has(extname(file).toLowerCase().slice(1))) return 0;
	const path = resolve(repositoryRoot, file);
	const buffer = await readFile(path).catch((error: NodeJS.ErrnoException) => {
		if (error.code === "ENOENT") return undefined;
		throw error;
	});
	if (buffer === undefined) return 0;
	if (buffer.includes(0)) return 0;
	const text = buffer.toString("utf8");
	const { out, replacements } = applyRules(text);
	if (out === text) return 0;
	if (!checkOnly) await writeFile(path, out, "utf8");
	return replacements;
}

const files = (changesOnly ? readChangedFiles() : readRepositoryFiles()).filter((file) => file !== ownPath);

if (files.length === 0) {
	console.log(`No ${scope} files for ai-cleanup`);
	process.exit(0);
}

let changedFiles = 0;
for (const file of files) {
	const replacements = await cleanFile(file);
	if (replacements === 0) continue;
	changedFiles++;
	console.log(
		checkOnly
			? `error: ${file}: ${replacements} AI typography replacement(s) needed; run quality-gates cleanup`
			: `changed: ${file} (${replacements})`,
	);
}

console.log(
	`ai-cleanup ${checkOnly ? "found" : "fixed"} ${changedFiles} of ${files.length} ${scope} file(s)`,
);
if (checkOnly && changedFiles > 0) process.exitCode = 1;
