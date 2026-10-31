# ai-review: 🐔 Müsli

Automatic pull request review with the GLM Coding Plan (Pi + `zai/glm-5.3`).
Reviews run on the self-hosted runner `s10-review` (a phone at home), so every GLM request leaves from the home IP.

## How a review works
1. Collect the PR title, description, discussion, changed files and diff with `gh`, plus the project rulebook
   (`CLAUDE.md` / `AGENTS.md` / optional `.github/ai-review.md`) from the **base** branch, so a PR cannot rewrite its own rules.
2. Five lens reviewers run **in parallel**, each a separate Pi call with read-only tools and its own checklists:

   | Lens | Looks for | Checklists (`vendor/code-review-skill/reference/`) |
   |---|---|---|
   | correctness | logic, edge cases, errors, state, concurrency | common bugs, error handling, async/concurrency, language guides for changed file types |
   | contracts | API/wire/data/migration compatibility, broken callers, tests | review best practices |
   | security | authn/authz, injection, secrets, abuse paths | security guide, SQL injection, XSS |
   | performance | complexity, N+1, unbounded growth, leaks | performance guide, N+1 |
   | simplicity | KISS: out-of-scope changes, over-engineering, too much code, not following project structure, new deps | universal code quality |

3. A synthesis pass verifies every candidate against the code, merges duplicates, re-checks earlier findings and writes the final review.
4. One PR review is posted as `github-actions[bot]`: header `🐔 Müsli's thoughts` with a verdict (only P0/P1 block), inline comments
   (max 25, with `suggestion` blocks for one-line fixes), severity + confidence + lenses per finding, and the model in the footer.
5. On re-runs, earlier findings are listed as fixed / still open / no longer applies / not a defect. Müsli replies
   "✅ Fixed in <sha>" on its own threads for fixed findings and resolves them; open ones stay open and are not re-posted.

6. Anti-loop rule: after the first review, new findings are only posted when they are P0/P1 or sit on lines
   changed since the previous review. Minor new remarks on already-reviewed code are dropped, so a PR converges
   instead of collecting fresh nits on every push. Suggested fixes must be minimal, keep the build and tests green,
   and never add abstractions, dependencies or refactors.

## Two review modes
The mode is picked from the PR's head branch (`release_from` input, default `staging`):

| PR | Mode | Focus |
|---|---|---|
| feature branch → `main` or `staging` | feature | one change in depth: correctness, KISS, scope, tests |
| `staging` → `main` | release | combined effect of already-reviewed features: interactions, migrations, config, deploy/rollback, client API breaks; P0-P2 only |

Both modes use the same five lenses and the same anti-loop rule. To change which branch counts as a release, set
`with: release_from: <branch>` in the project's caller workflow.

Müsli always writes in English and never approves or blocks in GitHub's review state (always `COMMENT`).
Cost on the Pro plan: about 180 credits and 4 minutes for a small PR (6 Pi calls).

## Files
| File | Purpose |
|---|---|
| `review.py` | Orchestration: PR data, parallel lenses, synthesis, posting, thread resolution |
| `prompts/base.md` | Shared lens reviewer instructions and output contract |
| `prompts/synthesis.md` | Lead reviewer instructions and final output contract |
| `vendor/code-review-skill/` | Checklists from [awesome-skills/code-review-skill](https://github.com/awesome-skills/code-review-skill) (MIT, pinned commit in `SOURCE.md`) |
| `run.sh` | Runner entrypoint: loads the GLM key and calls `review.py` |
| `.github/workflows/review.yml` | Reusable workflow called by projects |

## Adding Müsli to a project
1. Register an `s10-review` runner for that repo on s10 under `/opt/gha-review/runners/<repo>`.
2. Add `<project>/.github/workflows/ai-review.yml`:
   ```yaml
   name: AI review
   on:
     pull_request:
       branches: [main, staging]
       types: [opened, synchronize, reopened, ready_for_review]
   concurrency:
     group: ai-review-${{ github.event.pull_request.number }}
     cancel-in-progress: true
   permissions:
     contents: write       # only so Müsli can resolve its own fixed threads (GitHub requires it)
     pull-requests: write
   jobs:
     review:
       uses: tyfnacici/ai-review/.github/workflows/review.yml@main
   ```
3. Keep the project's `CLAUDE.md` up to date; it is the review rulebook.

## Settings (runner environment)
| Variable | Default | Meaning |
|---|---|---|
| `model` (workflow input) | `zai/glm-5.3` | Pi model spec |
| `THINKING` | `high` | Pi thinking level |
| `PARALLEL` | `5` | Lens calls running at once (lower it if the plan rate-limits) |

## s10 side
- User `gha-review` (system user, no docker/sudo), home `/opt/gha-review`.
- GLM Coding Plan key in `/opt/gha-review/.config/glm/zai.env` (mode 600). It never goes to GitHub.
- This repo is cloned at `/opt/gha-review/ai-review` with a read-only deploy key and synced at the start of every job.
