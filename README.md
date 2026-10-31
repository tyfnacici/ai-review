# ai-review: 🥣 Müsli

Automatic pull request review with the GLM Coding Plan (Pi + `zai/glm-5.3`).
Reviews run on the self-hosted runner `s10-review` (a phone at home), so every GLM request leaves from the home IP.

## What Müsli does
- Reads the PR title, description, discussion, changed files and diff, and opens surrounding code with read-only tools.
- Uses the project's **base-branch** `CLAUDE.md` / `AGENTS.md` (plus optional `.github/ai-review.md`) as the project rulebook. A PR cannot change its own review rules.
- Reviews correctness, contracts/integration, security, performance, tests, scope, and whether the description matches the code.
- Always writes in English, whatever the PR's language.
- Posts one PR review as `github-actions[bot]`:
  - header `🥣 Müsli's thoughts` with a verdict derived from severities (only P0/P1 block),
  - inline comments on changed lines (max 25), with GitHub `suggestion` blocks for small one-line fixes,
  - severity (P0-P3, nit) and confidence per finding,
  - a "Prior findings" section on re-runs: each earlier finding is re-checked as resolved / still open / obsolete / not a defect,
  - no duplicate inline comments on re-runs,
  - model, duration and commit in the footer.
- Never approves or blocks the PR in GitHub's review state (always `COMMENT`).

## Files
| File | Purpose |
|---|---|
| `review.py` | Collects PR data with `gh`, runs Pi, validates anchors, posts the review |
| `system.md` | Reviewer instructions and JSON output contract |
| `run.sh` | Runner entrypoint: loads the GLM key and calls `review.py` |
| `.github/workflows/review.yml` | Reusable workflow called by projects |
| `caller-example.yml` | Workflow to copy into a project |

## Adding Müsli to a project
1. Register an `s10-review` runner for that repo on s10 under `/opt/gha-review/runners/<repo>`.
2. Copy `caller-example.yml` to `<project>/.github/workflows/ai-review.yml`.
3. Keep the project's `CLAUDE.md` up to date; it is the review rulebook.

Only PRs targeting `main` are reviewed. Promotion PRs (`main` → `staging` → `production`) carry code that was already reviewed.

## Settings
| Variable | Default | Where |
|---|---|---|
| `model` input | `zai/glm-5.3` | caller workflow (`with: model: ...`) |
| `THINKING` | `high` | runner environment |

## s10 side
- User `gha-review` (system user, no docker/sudo), home `/opt/gha-review`.
- GLM Coding Plan key in `/opt/gha-review/.config/glm/zai.env` (mode 600). It never goes to GitHub.
- This repo is cloned at `/opt/gha-review/ai-review` with a read-only deploy key and synced at the start of every job.
