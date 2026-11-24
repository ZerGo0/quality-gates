import { readChangeBase, readGit } from "./repository-files.ts";

export type LineRange = { end: number; start: number };

type Hunk = { addedLines: number; removedLines: number; start: number };

type DiffState = {
	changedLines: Map<string, LineRange[]>;
	pendingContentLines: number;
	ranges: LineRange[];
};

const hunkHeader = /^@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/;
const fileHeader = "+++ ";
const newFileHeader = "+++ b/";

function hunkLength(count: string | undefined): number {
	return count === undefined ? 1 : Number(count);
}

function parseHunk(line: string): Hunk | undefined {
	const match = hunkHeader.exec(line);

	if (match === null) return undefined;

	return {
		addedLines: hunkLength(match[3]),
		removedLines: hunkLength(match[1]),
		start: Number(match[2]),
	};
}

function hunkRange({ addedLines, start }: Hunk): LineRange {
	return addedLines === 0 ? { end: start, start: start + 1 } : { end: start + addedLines - 1, start };
}

function startFile(changedLines: Map<string, LineRange[]>, header: string): LineRange[] {
	const ranges: LineRange[] = [];

	if (header.startsWith(newFileHeader)) changedLines.set(header.slice(newFileHeader.length), ranges);

	return ranges;
}

function readDiffLine(state: DiffState, line: string): void {
	if (state.pendingContentLines > 0) {
		state.pendingContentLines -= Number(/^[+-]/.test(line));

		return;
	}

	if (line.startsWith(fileHeader)) {
		state.ranges = startFile(state.changedLines, line);

		return;
	}

	const hunk = parseHunk(line);

	if (hunk === undefined) return;

	state.pendingContentLines = hunk.addedLines + hunk.removedLines;

	state.ranges.push(hunkRange(hunk));
}

export function readChangedLines(files: string[]): Map<string, LineRange[]> {
	const state: DiffState = { changedLines: new Map(), pendingContentLines: 0, ranges: [] };

	if (files.length === 0) return state.changedLines;

	const diff = readGit([
		"-c",
		"core.quotePath=false",
		"diff",
		"--unified=0",
		"--no-color",
		"--no-ext-diff",
		"--diff-filter=ACMR",
		readChangeBase(),
		"--",
		...files,
	]);

	for (const line of diff.split("\n")) readDiffLine(state, line);

	return state.changedLines;
}

export function touchesChangedLines(
	ranges: LineRange[] | undefined,
	start: number,
	end: number,
	linesAbove: number,
): boolean {
	return (
		ranges === undefined ||
		ranges.some((range) => start - linesAbove <= range.end && end >= range.start)
	);
}
