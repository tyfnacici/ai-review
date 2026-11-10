# ai-review

Automatic pull request review with the [Pi](https://github.com/badlogic/pi-mono) coding agent and any model Pi
supports (default `zai/glm-5.3-flash` on the GLM Coding Plan), plus a deterministic secret scan.
It runs on a self-hosted GitHub Actions runner on any Linux machine (x64 or arm64), so the model API key stays on that
machine and every model request leaves from its IP.

## What runs on a PR
| Job | What it does | Fails the check? |
|---|---|---|
| `secrets` | every push: [gitleaks](https://github.com/gitleaks/gitleaks) scans the lines the PR adds (values redacted in the log) | yes, on a leak |
| `dependencies` | every push: [osv-scanner](https://github.com/google/osv-scanner) scans the lockfiles the PR changes at the base and at the head and reports only the vulnerabilities the PR adds (`package-lock.json`, `Cargo.lock`, `yarn.lock`, `pnpm-lock.yaml`, `poetry.lock`, `uv.lock`, `Pipfile.lock`, `Gemfile.lock`, `composer.lock`, `go.mod`) | yes, on a new HIGH or CRITICAL one |
| `gate` | after every review and whenever a review thread is resolved or reopened: red while the bot has unresolved threads on the PR, green when all are resolved | yes, while threads are open |
| `review` | when the PR is opened / marked ready, and again each time you add the `ai-review` label: AI review posted as one PR review by `github-actions[bot]` | no, advisory only |

Pushes do not trigger the AI review. When you are done with the fixes, add the `ai-review` label to the PR; the
review runs and removes the label, so you can add it again later.

## How the AI review works
1. Collect the PR title and discussion with `gh` and the diff with `git` from the checkout (GitHub's diff API stops
   at 300 files, so PRs of any size work), plus the project rulebook
   (`CLAUDE.md` / `AGENTS.md` / optional `.github/ai-review.md`) from the **base** branch, so a PR cannot rewrite its own rules.
2. **First review of a PR:** one Pi agent with read-only tools reviews the PR from three perspectives, verifies its
   own findings against the code and writes the review. It reads the vendored checklists only when one helps it decide:

   | Perspective | Looks for | Checklists (`vendor/code-review-skill/reference/`) |
   |---|---|---|
   | correctness | logic, edge cases, errors, concurrency, API/data/migration compatibility, broken or missing tests | common bugs, error handling, async, best practices, language guides for changed file types |
   | security_performance | authn/authz, injection, secrets, abuse paths; complexity, N+1, leaks | security, SQL injection, XSS, performance, N+1 |
   | simplicity | KISS: out-of-scope changes, over-engineering, too much code, not following project structure, new deps, needless comments | universal code quality |

   One agent instead of one per perspective plus a merge step, because the GLM Coding Plan allows very few concurrent
   requests and a single agent that decides where to dig is what Cursor Bugbot and GitHub Copilot code review use too.
3. **Every later review (label):** one short follow-up call on the changes since the last review. Earlier findings are re-checked as
   fixed / still open / deferred / no longer applies / not a defect; the bot replies "Fixed in <sha>" on its own threads and
   resolves them. New findings must be P0-P2 on newly changed lines (P0 anywhere); from the 4th review on, only P0/P1.
   When nothing is left it says "No new comments", so a PR converges instead of looping.
   The first review also gives feedback on the project rulebook: with no `CLAUDE.md` / `AGENTS.md` it asks for one and
   lists what it should state; when the PR makes the rulebook outdated it says what to update.
   It also adds one line with the repository's open Dependabot alerts, and names the ones in lockfiles the PR changes
   (needs `vulnerability-alerts: read` in the calling workflow; without it the line is left out).
4. The review has a plain header (`Code review: Changes requested | Worth a look | Looks good`; only P0/P1 count as
   changes requested), inline comments (max 25, `suggestion` blocks for one-line fixes or deletions), severity, confidence
   and perspective per finding, and the model in the footer. P3/nit findings below 0.7 confidence are dropped.
5. Fixes must be minimal, keep the build and tests green, and never add abstractions, dependencies or refactors.
   Comments added in the diff that restate code, tell history, leak chat/task context or read like notes to an AI are flagged.

It always writes in English. The review is a `COMMENT`, except when nothing is left: no finding and none of its own
threads unresolved. Then it is an `APPROVE`. Approving needs the repository setting *Actions > General > Allow GitHub
Actions to create and approve pull requests*; without it the review falls back to a comment (the log says so). It never
requests changes.
The job log shows what the agent is doing as it happens: every file it reads or searches, every provider retry
(for example HTTP 429 "Too much concurrency" from the GLM Coding Plan), and per call the turns, tool calls and tokens.

## Time budget and large PRs
A review finishes within `time_budget` (15 minutes). Two minutes before the end the agent is told to stop exploring and
return what it has verified; at the end it is stopped. Only one review per machine uses the model at a time (a lock in
`run.sh`); a review waiting for the lock starts its budget once it gets it.

The diff goes into the prompt up to `batch_chars` (120k characters), code first, then tests, docs and styles, then
data. Lockfiles, binaries and minified files are skipped; data files are cut at 30k characters, and when one folder has
10 or more changed data files of one type only 2 go into the diff and the rest are listed for the agent to open.

- **Detailed review:** a diff that fits is reviewed line by line, all severities.
- **Broad review of a large PR:** otherwise the agent gets code and tests only. Each file keeps a fair share of its
  diff; when that share would be too small, every file is shown as an outline (its top-level added lines:
  declarations, exports, routes). Docs, styles and data are listed. The agent looks at architecture, contracts,
  migrations, security-sensitive paths and error handling, opens the riskiest files itself (at most 5), and reports
  P0-P2 only.

One runner reviews one PR at a time; more runners on the machine (`RUNNER_NAME=<host>-<repo>-2 ./install.sh
<owner>/<repo>`) take the next PRs, check them out and wait for the model lock.

## Two review modes
The mode is picked from the PR's head branch (`release_from` input, default `staging`):

| PR | Mode | Focus |
|---|---|---|
| feature branch → `main` or `staging` | feature | one change in depth |
| `staging` → `main` | release | combined effect of already-reviewed features: interactions, migrations, config, deploy/rollback, client API breaks; P0-P2 only |

## Files
| File | Purpose |
|---|---|
| `review.py` | Orchestration: PR data, the review or follow-up agent run (Pi RPC mode), posting, thread resolution; `--gate` only lists the unresolved threads |
| `prompts/review.md` | Reviewer instructions and output contract |
| `prompts/followup.md` | Follow-up review instructions for later pushes |
| `vendor/code-review-skill/` | Checklists from [awesome-skills/code-review-skill](https://github.com/awesome-skills/code-review-skill) (MIT, pinned commit in `SOURCE.md`) |
| `run.sh` | Runner entrypoint: loads the API key file and calls `review.py` |
| `deps.py` | Lockfile check of the `dependencies` job: new known vulnerabilities in a PR compared with its base |
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
the runner updates itself, and the reviewer code is synced from `main` before every job. gitleaks and osv-scanner stay pinned to checked versions on purpose.

## Adding it to a project
1. Install a runner for the repository (above).
2. Add `<project>/.github/workflows/ai-review.yml`:
   ```yaml
   name: AI review
   on:
     pull_request:
       branches: [main, staging]
       types: [opened, synchronize, reopened, ready_for_review, labeled]
     pull_request_review_thread:
       types: [resolved, unresolved]
   concurrency:
     group: ai-review-${{ github.event.pull_request.number }}-${{ github.event.action }}-${{ github.event.label.name }}
     cancel-in-progress: true
   permissions:
     contents: write       # only so the bot can resolve its own fixed threads (GitHub requires it)
     pull-requests: write
     vulnerability-alerts: read   # lets the review mention open Dependabot alerts
   jobs:
     review:
       uses: tyfnacici/ai-review/.github/workflows/review.yml@main
       with:
         runs_on: '["self-hosted", "ai-review"]'
         model: zai/glm-5.3-flash
   ```
3. Create the label once: `gh label create ai-review -R <owner>/<repo> --description "Run the AI review again"`.
4. Keep the project's `CLAUDE.md` up to date; it is the review rulebook.
5. To make the merge button wait for the gate (needs GitHub Pro for a private repository, or a public one): in the
   branch protection of `main` require the `review / gate` status check and turn on *Require conversation resolution
   before merging*. Without branch protection the gate is red or green on the PR, but nothing blocks the merge.
6. Turn on Dependabot alerts for the repo (alerts only; no automatic PRs needed). The `dependencies` job needs nothing else.

## Settings (workflow inputs)
| Input | Default | Meaning |
|---|---|---|
| `runs_on` | `["self-hosted", "s10-review"]` | Runner labels (JSON list) |
| `model` | `zai/glm-5.3-flash` | Pi model spec (`provider/model`); `pi --list-models` lists them, e.g. `zai/glm-5.3` for deeper and slower reviews |
| `thinking` | `medium` | Pi thinking level (`off` ... `max`); lower is faster |
| `parallel` | `1` | Follow-up calls at once for a follow-up larger than one batch; keep 1 on plans with low concurrency |
| `batch_chars` | `120000` | Diff characters per review call; larger diffs are split into batches |
| `time_budget` | `15` | Minutes for the whole review; calls still running at the end are stopped (`0` = no limit) |
| `release_from` | `staging` | Head branch that gets a release review |
| `trigger_label` | `ai-review` | Label that runs the review again |

## Original setup
The defaults match the author's runner `s10-review`: user `gha-review` with home `/opt/gha-review`, the key in
`~/.config/glm/zai.env` (still read by `run.sh`), and this repository cloned with a read-only deploy key.
