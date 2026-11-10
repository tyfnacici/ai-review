# ai-review

Automatic pull request review with the GLM Coding Plan (Pi + `zai/glm-5.3`), plus a deterministic secret scan.
Everything runs on the self-hosted runner `s10-review` (a phone at home), so every GLM request leaves from the home IP.

## What runs on a PR
| Job | What it does | Fails the check? |
|---|---|---|
| `secrets` | every push: [gitleaks](https://github.com/gitleaks/gitleaks) scans the lines the PR adds (values redacted in the log) | yes, on a leak |
| `review` | when the PR is opened / marked ready, and again each time you add the `ai-review` label: AI review posted as one PR review by `github-actions[bot]` | no, advisory only |

Pushes do not trigger the AI review. When you are done with the fixes, add the `ai-review` label to the PR; the
review runs and removes the label, so you can add it again later.

## How the AI review works
1. Collect the PR title and discussion with `gh` and the diff with `git` from the checkout (GitHub's diff API stops
   at 300 files, so PRs of any size work), plus the project rulebook
   (`CLAUDE.md` / `AGENTS.md` / optional `.github/ai-review.md`) from the **base** branch, so a PR cannot rewrite its own rules.
2. **First review of a PR:** three lens reviewers run in parallel, each a separate Pi call with read-only tools and its own checklists:

   | Lens | Looks for | Checklists (`vendor/code-review-skill/reference/`) |
   |---|---|---|
   | correctness | logic, edge cases, errors, concurrency, API/data/migration compatibility, broken or missing tests | common bugs, error handling, async, best practices, language guides for changed file types |
   | security_performance | authn/authz, injection, secrets, abuse paths; complexity, N+1, leaks | security, SQL injection, XSS, performance, N+1 |
   | simplicity | KISS: out-of-scope changes, over-engineering, too much code, not following project structure, new deps, needless comments | universal code quality |

   A synthesis pass verifies every candidate against the code, merges duplicates and writes the review.
3. **Every later review (label):** one short follow-up call on the changes since the last review. Earlier findings are re-checked as
   fixed / still open / deferred / no longer applies / not a defect; the bot replies "Fixed in <sha>" on its own threads and
   resolves them. New findings must be P0-P2 on newly changed lines (P0 anywhere); from the 4th review on, only P0/P1.
   When nothing is left it says "No new comments", so a PR converges instead of looping.
4. The review has a plain header (`Code review: Changes requested | Worth a look | Looks good`; only P0/P1 count as
   changes requested), inline comments (max 25, `suggestion` blocks for one-line fixes or deletions), severity, confidence
   and lenses per finding, and the model in the footer. P3/nit findings below 0.7 confidence are dropped.
5. Fixes must be minimal, keep the build and tests green, and never add abstractions, dependencies or refactors.
   Comments added in the diff that restate code, tell history, leak chat/task context or read like notes to an AI are flagged.

It always writes in English and never approves or blocks in GitHub's review state (always `COMMENT`).
Cost on the Pro plan: about 150 credits and 3-4 minutes for the first review of a small PR, about 10 credits and
under a minute for a follow-up.

## Large PRs
Every changed file is reviewed. When the diff is larger than about 120k characters it is split into batches by file
(a single huge file diff is split into parts), and every lens reviews every batch; the synthesis pass then sees all
candidates. Follow-ups are batched the same way. Exceptions, listed in the review footer: lockfiles, binaries and
minified files are skipped; data files (`.json`, `.csv`, `.svg`, ...) are cut at 30k characters, and when one folder
has 10 or more changed data files of one type only 2 go into the diff and the rest are listed for the reviewer to read.
A PR with a 450k-character diff takes 4 batches (12 lens calls); one with 400+ files and 4.6M characters, mostly
data, about 19 batches (57 calls) and can run for a few hours with 3 calls in parallel.

## Two review modes
The mode is picked from the PR's head branch (`release_from` input, default `staging`):

| PR | Mode | Focus |
|---|---|---|
| feature branch → `main` or `staging` | feature | one change in depth |
| `staging` → `main` | release | combined effect of already-reviewed features: interactions, migrations, config, deploy/rollback, client API breaks; P0-P2 only |

## Files
| File | Purpose |
|---|---|
| `review.py` | Orchestration: PR data, parallel lenses, synthesis or follow-up, posting, thread resolution |
| `prompts/base.md` | Lens reviewer instructions and output contract |
| `prompts/synthesis.md` | Lead reviewer instructions and final output contract |
| `prompts/followup.md` | Follow-up review instructions for later pushes |
| `vendor/code-review-skill/` | Checklists from [awesome-skills/code-review-skill](https://github.com/awesome-skills/code-review-skill) (MIT, pinned commit in `SOURCE.md`) |
| `run.sh` | Runner entrypoint: loads the GLM key and calls `review.py` |
| `.github/workflows/review.yml` | Reusable workflow (`secrets` + `review` jobs) called by projects |

## Adding it to a project
1. Register an `s10-review` runner for that repo on s10 under `/opt/gha-review/runners/<repo>`.
2. Add `<project>/.github/workflows/ai-review.yml`:
   ```yaml
   name: AI review
   on:
     pull_request:
       branches: [main, staging]
       types: [opened, synchronize, reopened, ready_for_review, labeled]
   concurrency:
     group: ai-review-${{ github.event.pull_request.number }}-${{ github.event.action }}-${{ github.event.label.name }}
     cancel-in-progress: true
   permissions:
     contents: write       # only so the bot can resolve its own fixed threads (GitHub requires it)
     pull-requests: write
   jobs:
     review:
       uses: tyfnacici/ai-review/.github/workflows/review.yml@main
   ```
3. Create the label once: `gh label create ai-review -R <owner>/<repo> --description "Run the AI review again"`.
4. Keep the project's `CLAUDE.md` up to date; it is the review rulebook.
5. Turn on Dependabot alerts for the repo (alerts only; no automatic PRs needed).

## Settings (runner environment)
| Variable | Default | Meaning |
|---|---|---|
| `model` (workflow input) | `zai/glm-5.3` | Pi model spec |
| `THINKING` | `high` | Pi thinking level |
| `PARALLEL` | `3` | Lens calls running at once |

## s10 side
- User `gha-review` (system user, no docker/sudo), home `/opt/gha-review`.
- GLM Coding Plan key in `/opt/gha-review/.config/glm/zai.env` (mode 600). It never goes to GitHub.
- This repo is cloned at `/opt/gha-review/ai-review` with a read-only deploy key and synced at the start of every job.
- gitleaks is downloaded once (pinned version, SHA-256 checked) to `/opt/gha-review/bin`.
