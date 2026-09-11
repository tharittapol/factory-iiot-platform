# Contributing

The steps for getting a change from your working tree onto `main`, and why each
one is there.

## The workflow

```bash
1.  ./scripts/dev-check.sh                   # gate
2.  git switch -c fix/whatever               # branch FIRST
3.  git add -A
4.  git diff --cached                        # review what you staged
5.  git commit
6.  git push -u origin fix/whatever
7.  gh pr create
8.  gh pr checks <n> --watch                 # wait for CI green
9.  gh pr merge --rebase --delete-branch
10. git switch main && git pull
```

## Why the order matters

**Branch before you commit (2 before 5).** Committing first puts the commit on
your local `main`, and you then have to `git reset` it back off. Branch first
and the commit lands where it belongs.

**Stage before you review (3 before 4).** `git diff --cached` shows only staged
changes. Run it before `git add` and it prints nothing, which reads as clean.

**Wait for CI before merging (8 before 9).** This is the reason for branching
rather than pushing straight to `main`. CI runs on `pull_request`, so the PR is
what triggers it. A push to a branch with no PR triggers nothing, because
`on: push` is scoped to `main`.

**Sync after merging (10).** `pull.ff = only` makes `git pull` refuse rather
than silently create a merge commit. Syncing right after the merge keeps your
local `main` from falling behind and the next pull from refusing.

## What the local gate covers

`make check` is the fast loop while iterating: lint, format check and tests.
`./scripts/dev-check.sh` is the pre-push gate and covers more. Neither covers
everything CI does.

| CI job | Covered locally by |
|---|---|
| `lint-and-test` | `make check` and `dev-check.sh`, fully |
| `secret-scan` | `dev-check.sh` for binaries and identifiers; `make scan` adds gitleaks |
| `config-contract` | `dev-check.sh`, partially |
| `systemd-unit` | `dev-check.sh`, fully |
| `build-and-smoke` | nothing |

Three gaps, so a green local run does not mislead you:

- **gitleaks may not be installed.** `make scan` then prints
  `(gitleaks not installed, skipped)` and still reports `clean`. CI runs it for
  real.
- **The identifier scan needs `.scan-patterns`.** Without it the local scan
  prints `skipped` and passes. CI reads the `SCAN_PATTERNS` secret and fails if
  it is missing.
- **The local config check is weaker than CI's.** `dev-check.sh` loads
  `tags.yaml` but does not assert the required-tag list (`cmd_word`,
  `gw_heartbeat` and the rest). A dropped or renamed tag passes locally and
  fails in CI.
- **Nothing local builds the image.** If you touched the `Dockerfile`,
  `main.py` or the Modbus map, run it yourself:

  ```bash
  make build && make up && make verify
  make down
  ```

## Identifier patterns

The customer identifiers the scan looks for are kept out of the repo, because a
tracked list of them would publish exactly what the scan protects. They live in
two places that must be kept in step:

- `.scan-patterns` at the repo root, gitignored, one extended regex per line.
  Ask a maintainer for its contents.
- The `SCAN_PATTERNS` Actions secret, which CI reads.

After changing the local file, update the secret:

```bash
gh secret set SCAN_PATTERNS < .scan-patterns
```

A match is reported by file name only, so a hit never prints an identifier
into a public CI log.

## Git configuration

The workflow assumes fast-forward-only pulls:

```bash
git config --global pull.ff only
```

When `git pull` refuses because `main` has moved, resolve it deliberately with
`git pull --rebase`.
