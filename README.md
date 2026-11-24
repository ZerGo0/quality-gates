# quality-gates

Agent quality gates for any Git repository: aislop, anti-slop (oxlint with `@nkzw/oxlint-config`, `@interfere/landmines`, and local rules), ai-cleanup, fallow, and SlopO duplicate detection. Each gate checks only the current changes, and fails only on findings the changes introduce, so it also works in repositories you do not own.

## Install

```sh
git clone https://github.com/ZerGo0/quality-gates.git ~/code/quality-gates
ln -s ~/code/quality-gates/bin/quality-gates ~/.local/bin/quality-gates
```

Requires `git`, Node.js 24 or later, `pnpm`, `uv`, Python 3.12 or later, and `curl`. Dependencies install on first use and again when a lockfile changes.

SlopO is optional. Until you run `quality-gates setup`, the SlopO gate prints a warning and passes. Setup downloads llama.cpp and the embedding model (about 500 MB) into `${XDG_CACHE_HOME:-~/.cache}/quality-gates`. All repositories share that copy.

## Use

Run from anywhere inside a repository:

```sh
quality-gates run                  # all gates on current changes
quality-gates run --base <ref>     # all gates on changes since <ref>
quality-gates run --all            # all gates on the entire repository
quality-gates cleanup              # fix AI typography in all files
quality-gates cleanup --changes    # fix AI typography in changed lines only
```

On a branch, current changes are all files that differ from the merge-base with the default branch (`origin/HEAD`, `origin/main`, `origin/master`, then local `main` or `master`), committed or not. On `main` and `master`, they are the uncommitted changes. `--base <ref>` compares with `<ref>` instead. anti-slop and ai-cleanup report only findings on changed lines (anti-slop also on the line below a change); old findings elsewhere in a changed file do not fail the gate.

Reports and the SlopO index go to `${XDG_CACHE_HOME:-~/.cache}/quality-gates/repositories/<name>-<hash>/`, one folder for each worktree path. The run summary prints the report paths. The gates write nothing into the repository.

## Per-repository config

All files are optional:

- `.aislopignore`: paths that anti-slop and ai-cleanup skip
- `.aislop/config.yml`, `.fallowrc.jsonc`: aislop and fallow config
- `.quality-gates/slopo.conf.yaml`: SlopO profile. Without it, `slopo/profiles/default.conf.yaml` applies. Start from a copy of that file. quality-gates sets `db_file`, `report_dir`, and `ignore_file` itself.
- `.quality-gates/slopo.ignore.txt`: dismissed SlopO cluster hashes. Without it, SlopO keeps them in the repository cache folder. Move that file here to commit it.

## Update

```sh
git -C ~/code/quality-gates pull
```
