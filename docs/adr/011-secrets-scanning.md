# ADR-011: detect-secrets in pre-commit and CI

**Decision.** `detect-secrets` runs as a pre-commit hook and in CI against a committed
baseline; `.env` is gitignored and only `.env.example` is tracked. `make secrets-scan` (part
of `make check`, and the CI step) runs `detect-secrets-hook` over the tracked files, which fails
on a new secret but never rewrites the baseline, so a clean checkout stays clean. To accept a
reviewed false positive, regenerate the baseline with `uv run detect-secrets scan --baseline
.secrets.baseline` and commit it.

**Alternatives.** (a) gitleaks: more widely used in enterprise pipelines and faster, but it is
a Go binary that pre-commit builds or downloads, which adds a toolchain to a Python repo.
(b) Rely on gitignore alone: catches the `.env` file, not a key pasted into a test or a notebook.

**Why this.** It is pip-installable and managed by uv like everything else, runs in CI with no
extra setup, and it has already earned its place: it flagged a fake API key literal in a test
(allowlisted with a pragma) and the hex gold-result hashes in the eval dataset (made
self-evidently non-secret with a `gold:` prefix). In a production repo gitleaks or GitHub's
push protection would run alongside it.
