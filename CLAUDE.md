# ai-review

Self-hosted GitHub pull request review: a Pi agent review (`review.py`), deterministic secret and dependency scans, and a
gate check. README.md explains what it does, how it works and how to install it; its "Files" table is the code map.

## Conventions
- Python 3 standard library only (3.9+). Shell scripts are POSIX `sh`, except `install.sh` (bash).
- KISS: the smallest change that solves the problem; no new dependencies, options or abstractions.
- Almost no code comments; only where the reason is not obvious.
- Everything in the repository is English: code, docs, prompts and the review text the bot posts. The review is plain text
  with no branding or emoji.
- Behaviour changes update README.md in the same change.

## Rules
- Projects use the reusable workflow at `@main` and the runner resets its reviewer clone to `origin/main` before every
  job, so `main` must always work. Change it only through a pull request; never push to `main` directly.
- Never commit API keys or key file paths of a real machine. Keys live in `~/.config/ai-review/env` of the runner user.
- Pinned versions (gitleaks, osv-scanner, GitHub actions) are updated deliberately, with the new SHA-256 or commit.

## Testing
There is no test suite. Run `python3 review.py --repo <owner>/<repo> --pr <n> --workdir <checkout at PR head> --dry-run`
against a real PR (it prints the review instead of posting it), `python3 review.py --gate ...` for the gate, and
`python3 deps.py --osv <osv-scanner> --base origin/main` for the dependency check.
