# quality-gates

Agent quality gates for any Git repository: aislop, anti-slop (oxlint rules), ai-cleanup, fallow, and SlopO duplicate detection. Each gate checks only the current changes.

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
quality-gates cleanup              # fix AI typography in all files
quality-gates cleanup --changes    # fix AI typography in changed files only
```

Reports go to `.task/aislop/` and the SlopO index goes to `.slopo/repository/`. Add both to the repository's `.gitignore`.

## Per-repository config

All files are optional:

- `.aislopignore`: paths that anti-slop and ai-cleanup skip
- `.aislop/config.yml`, `.fallowrc.jsonc`: aislop and fallow config
- `.quality-gates/slopo.conf.yaml`: SlopO profile. Without it, `slopo/profiles/default.conf.yaml` applies. Start from a copy of that file. Keep `db_file`, `report_dir`, and `ignore_file` unchanged.
- `.quality-gates/slopo.ignore.txt`: dismissed SlopO cluster hashes. SlopO creates it on the first run. Commit it.

## Update

```sh
git -C ~/code/quality-gates pull
```
