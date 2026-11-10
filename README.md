# ai-review

Automatic pull request review with the [Pi](https://github.com/badlogic/pi-mono) coding agent and any model Pi
supports (default `zai/glm-5.3-flash` on the GLM Coding Plan), plus a deterministic secret scan.
It runs on a self-hosted GitHub Actions runner on any Linux machine (x64 or arm64), so the model API key stays on that
machine and every model request leaves from its IP.

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
   The first review also gives feedback on the project rulebook: with no `CLAUDE.md` / `AGENTS.md` it asks for one and
   lists what it should state; when the PR makes the rulebook outdated it says what to update.
4. The review has a plain header (`Code review: Changes requested | Worth a look | Looks good`; only P0/P1 count as
   changes requested), inline comments (max 25, `suggestion` blocks for one-line fixes or deletions), severity, confidence
   and lenses per finding, and the model in the footer. P3/nit findings below 0.7 confidence are dropped.
5. Fixes must be minimal, keep the build and tests green, and never add abstractions, dependencies or refactors.
   Comments added in the diff that restate code, tell history, leak chat/task context or read like notes to an AI are flagged.

It always writes in English and never approves or blocks in GitHub's review state (always `COMMENT`).
Cost on the GLM Coding Plan Pro: about 150 credits and 3-4 minutes for the first review of a small PR, about 10
credits and under a minute for a follow-up. The job log shows each review call as it starts and finishes, with how many
are done.

## Large PRs
A review runs all its model calls in one wave and finishes within `time_budget` (15 minutes), whatever the PR size:
the lens calls get the first 10 minutes and the synthesis the rest. A call still running at its deadline is stopped;
the review is then posted with what finished and says which part is incomplete (unverified lens findings when the
synthesis is stopped). The diff is split into batches of about 120k characters (`batch_chars`), code
first, then tests, docs and styles, then data. Lockfiles, binaries and minified files are skipped; data files are cut
at 30k characters, and when one folder has 10 or more changed data files of one type only 2 go into the diff and the
rest are listed for the reviewers to open.

- **Detailed review:** when every batch fits in one wave (`parallel` / 3 batches for the lenses, `parallel` for a
  follow-up), every lens reviews every batch. With the default `parallel: 3` that is a diff up to about 120k characters.
- **Broad review of a large PR:** otherwise the reviewers get code and tests only, in the same number of batches. Each
  file keeps a fair share of its diff; when that share would be too small, every file is shown as an outline (its
  top-level added lines: declarations, exports, routes). Docs, styles and data are listed. The reviewers look at
  architecture, contracts, migrations, security-sensitive paths and error handling, open the riskiest files
  themselves (at most 5 files), and report P0-P2 only. The long checklists are left out to save time. A 437-file
  PR with 4.6M characters of diff becomes 3 lens calls instead of 57.

Raising `parallel` (if your provider's rate limit allows) makes more PRs fit a detailed review. One runner reviews one
PR at a time; to review several PRs at once, install more runners on the machine with the same label
(`RUNNER_NAME=<host>-<repo>-2 ./install.sh <owner>/<repo>`). Each running review makes up to `parallel` model calls
at once, so 3 runners with `parallel: 3` make up to 9.

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
| `run.sh` | Runner entrypoint: loads the API key file and calls `review.py` |
| `update-pi.sh` | Installs or updates Pi to its latest release; runs at the start of every review job |
| `install.sh` | Sets up a self-hosted runner for ai-review on a Linux machine |
| `.github/workflows/review.yml` | Reusable workflow (`secrets` + `review` jobs) called by projects |

## Install on a Linux machine
Needs a Linux machine (x64 or arm64) with systemd, `sudo`, `git`, `curl`, `python3` (3.9+) and
[`gh`](https://cli.github.com) logged in as an admin of the repository. The machine only makes outgoing connections.

```sh
git clone https://github.com/tyfnacici/ai-review.git && cd ai-review
./install.sh <owner>/<repo>          # or ./install.sh <org> for every repository of an organization
```

The script creates a system user `ai-review` (home `/opt/ai-review`, no sudo), installs the latest Pi, clones this
repository, and registers and starts a runner with the label `ai-review`. Then put the API key of your model provider
into `/opt/ai-review/.config/ai-review/env` (mode 600; it never goes to GitHub), for example:

```sh
sudo -u ai-review nano /opt/ai-review/.config/ai-review/env
# ZAI_API_KEY=...          for zai/... models
# ANTHROPIC_API_KEY=...    for anthropic/... models
```

`pi --help` lists every provider and its variable. Overrides: `LABEL`, `RUNNER_NAME`, `KEY_FILE` (an env file to
install), `AI_REVIEW_URL` (your fork), `RUNNER_USER`, `RUNNER_HOME`. Run it again for another repository; the user,
Pi and key are shared. Pi updates itself at the start of every review job (the previous version stays in `~/pi.old`),
the runner updates itself, and the reviewer code is synced from `main` before every job. gitleaks stays pinned to a
checked version on purpose.

## Adding it to a project
1. Install a runner for the repository (above).
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
       with:
         runs_on: '["self-hosted", "ai-review"]'
         model: zai/glm-5.3-flash
   ```
3. Create the label once: `gh label create ai-review -R <owner>/<repo> --description "Run the AI review again"`.
4. Keep the project's `CLAUDE.md` up to date; it is the review rulebook.
5. Turn on Dependabot alerts for the repo (alerts only; no automatic PRs needed).

## Settings (workflow inputs)
| Input | Default | Meaning |
|---|---|---|
| `runs_on` | `["self-hosted", "s10-review"]` | Runner labels (JSON list) |
| `model` | `zai/glm-5.3-flash` | Pi model spec (`provider/model`); `pi --list-models` lists them, e.g. `zai/glm-5.3` for deeper and slower reviews |
| `thinking` | `high` | Pi thinking level (`off` ... `max`); lower is faster |
| `parallel` | `3` | Review calls running at once; a PR whose batches need more than one wave gets a broad review |
| `batch_chars` | `120000` | Diff characters per review call; larger diffs are split into batches |
| `time_budget` | `15` | Minutes for the whole review; calls still running at the end are stopped (`0` = no limit) |
| `release_from` | `staging` | Head branch that gets a release review |
| `trigger_label` | `ai-review` | Label that runs the review again |

## Original setup
The defaults match the author's runner `s10-review`: user `gha-review` with home `/opt/gha-review`, the key in
`~/.config/glm/zai.env` (still read by `run.sh`), and this repository cloned with a read-only deploy key.
