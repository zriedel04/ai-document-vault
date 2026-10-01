# Contributing

## Keeping private data out of Git

This project handles personal documents, so three layers stop them reaching the repository:

1. `.gitignore` keeps vault data, databases, logs, secrets and local config untracked.
2. `scripts/check_repo_safety.py` runs as a pre-commit hook (on the staged version of each file) and in CI (on all tracked files and every file version in the full history).
3. gitleaks scans for secrets in pre-commit and in CI (full history).

The safety check rejects:

| What | Why |
| --- | --- |
| Databases (`.db`, `.sqlite*`, or SQLite content under any name) | Vault data |
| `.env` files, `config.toml`, `*.local.toml` | Secrets and local settings (only `.env.example`, `config.example.toml`, `config.demo.toml` allowed) |
| Keys and certificates, model weights, log files | Secrets, size, content leaks |
| Files in `blobs/`, `derived/`, `backups/`, `exports/`, `logs/`, `eval/private/` | Private data folders |
| Documents and images outside `tests/**/fixtures/`, `demo/corpus/` (images also allowed in `docs/images/` and frontend asset folders) | Synthetic data only |
| Files over 1 MiB (5 MiB for fixtures and lock files) | Keeps the repository small |

Never weaken the check to get a commit through. Put synthetic files in the fixture or demo folders instead.

If private data is ever committed, deleting the file is not enough: it stays in history. Stop, tell the maintainer, and rewrite history before anything is shared.

GitHub's free plan has no branch protection or push protection for private repositories, so these local and CI checks are the safeguard. Run them.

## Development setup

```
py -m pip install --user pre-commit
pre-commit install
```

## Workflow

1. Pick or open an issue.
2. Create a branch named `<type>/<description>` (never commit to `main`).
3. Make small commits using Conventional Commits (`feat:`, `fix:`, `docs:`, `test:`, `chore:`, `ci:`).
4. Run `pre-commit run --all-files` and the tests.
5. Open a pull request using the template, covering what changed, why, architectural decisions, files and components affected, and how it was tested.
6. An independent reviewer (OpenAI Codex, arranged by the maintainer) reviews the PR. Reviews run automatically or by commenting `@codex review` on the PR. `AGENTS.md` holds the reviewer guidelines (its `## Review guidelines` section): what blocks a merge, what to check, and how findings are reported. Each finding is evaluated on its merits: legitimate ones are fixed, and disagreement is explained in the PR rather than ignored.
7. The maintainer merges only after explicitly approving.

Docs, ADRs, tests, `AGENTS.md` and `CLAUDE.md` are updated in the same PR as the change, so a reviewer needs no conversation history. After a major phase, the whole phase may be audited against the architecture plan and ADRs.

## Licences

Allowed: MIT, BSD-2-Clause, BSD-3-Clause, Apache-2.0, ISC, PSF-2.0, Python-2.0, Unlicense, 0BSD, Zlib, CC0-1.0.
Case by case, with a note in the PR: LGPL, MPL, and custom model licences.
Not allowed: GPL, AGPL, SSPL, non-commercial licences.

A written licence audit is done before the repository is made public.

## Architecture decisions

Significant decisions are recorded as ADRs in `docs/decisions/` (see its README for the template, statuses and rules). Changing an accepted decision needs a new ADR that supersedes it and the maintainer's approval. Hardware-dependent choices stay Proposed until their spike has run on the maintainer's Windows PC. `python scripts/check_adrs.py` checks ADR structure, statuses and the index.

## AI-assisted development

Much of this project is written with AI tools. All changes go through pull requests, tests and review, and the documentation and tests are meant to let the maintainer understand and explain every major system.
