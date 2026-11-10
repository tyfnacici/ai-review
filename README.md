# ai-review

Automatic pull request review for GitHub, running on your own machine. A coding agent ([Pi](https://github.com/badlogic/pi-mono))
reads the PR with read-only tools and posts one review; deterministic checks scan the PR for leaked secrets and newly
introduced vulnerable dependencies; a gate check stays red until every review thread is resolved.

It runs as a self-hosted GitHub Actions runner on any Linux machine (x64 or arm64) with a hosted model or one running on
your own hardware (Ollama, llama.cpp, LM Studio, vLLM). Your code is sent to the model you choose and nowhere else, and
the API key never leaves that machine.

## The problem

- **Review is the bottleneck.** On a small team or a solo project nobody has the time, and the review that does happen is
  shallow on big PRs.
- **Hosted AI reviewers are another party.** They need access to your repository, bill per seat, and you cannot change
  what they look at or which model they use.
- **Mistakes the machine can catch are not caught.** Secrets end up in diffs, a lockfile change brings in a package with a
  known vulnerability, and findings are left unresolved while the PR is merged anyway.
- **Reviews do not converge.** An AI that comments on every push produces noise; one that never re-checks its own findings
  leaves stale threads.

ai-review is a small, readable answer to this: one agent that digs where the risk is, deterministic scans for the parts
that do not need a model, a bounded run time, and a PR that converges to "nothing left".

## What runs on a PR

| Job | When | What it does | Fails the check? |
|---|---|---|---|
| `secrets` | every push | [gitleaks](https://github.com/gitleaks/gitleaks) scans the lines the PR adds; values are redacted in the log | yes, on a leak |
| `dependencies` | every push | [osv-scanner](https://github.com/google/osv-scanner) scans the lockfiles the PR changes at the base and at the head and reports only the vulnerabilities the PR adds. Supported: `package-lock.json`, `npm-shrinkwrap.json`, `yarn.lock`, `pnpm-lock.yaml`, `Cargo.lock`, `poetry.lock`, `uv.lock`, `Pipfile.lock`, `Gemfile.lock`, `composer.lock`, `go.mod` | yes, on a new HIGH or CRITICAL one |
| `review` | PR opened or marked ready, and each time the `ai-review` label is added | the AI review, posted as one PR review by `github-actions[bot]` | no, advisory |
| `gate` | after every review, push and label | red while the bot has unresolved review threads on the PR, green when all are resolved | yes, while threads are open |

```
PR opened / ready      secrets, dependencies, review (full review), gate
push                   secrets, dependencies, gate
label "ai-review"      review (follow-up on the new commits), gate
```

Pushes do not start the AI review, so it does not comment on half-finished work. When the fixes are done, add the
`ai-review` label; the review runs and removes the label, so it can be added again later. Drafts and PRs from forks are
skipped (a fork must never reach a self-hosted runner).

## How the AI review works

1. **Collect.** The PR title and discussion come from `gh`, the diff from `git` in the checkout (GitHub's diff API stops at
   300 files, so PRs of any size work), and the project rulebook (`CLAUDE.md`, `AGENTS.md`, optional
   `.github/ai-review.md`) from the **base** branch, so a PR cannot rewrite its own rules.
2. **First review.** One Pi agent with read-only tools (`read`, `grep`, `find`, `ls`) reviews the PR from three
   perspectives, verifies its own findings against the code and writes the review. It reads the vendored checklists only
   when one helps it decide.

   | Perspective | Looks for | Checklists (`vendor/code-review-skill/reference/`) |
   |---|---|---|
   | correctness | logic, edge cases, errors, concurrency, API/data/migration compatibility, broken or missing tests | common bugs, error handling, async, best practices, language guides for changed file types |
   | security_performance | authn/authz, injection, secrets, abuse paths; complexity, N+1, leaks | security, SQL injection, XSS, performance, N+1 |
   | simplicity | out-of-scope changes, over-engineering, too much code, not following the project structure, new dependencies, needless comments | universal code quality |

   One agent instead of one per perspective plus a merge step: model plans often allow very few concurrent requests, and a
   single agent that decides where to dig is also how Cursor Bugbot and GitHub Copilot code review work.
3. **Follow-ups.** Every later review (label) is one short call on the changes since the last review. Earlier findings
   are re-checked as fixed, still open, deferred, no longer applying or not a defect; the bot replies "Fixed in `<sha>`" on
   its own threads and resolves them. New findings must be P0-P2 on newly changed lines (P0 anywhere); from the 4th review
   on only P0/P1. When nothing is left it says "No new comments", so a PR converges instead of looping.
4. **Rulebook feedback.** The first review also comments on the project rulebook: with no `CLAUDE.md` / `AGENTS.md` it asks for
   one and lists what it should state; when the PR makes the rulebook outdated it says what to update.
5. **Dependabot.** The first review adds one line with the repository's open Dependabot alerts and names the ones in
   lockfiles the PR changes (needs `vulnerability-alerts: read` in the calling workflow; without it the line is left out).

### The review

A plain header (`Code review: Changes requested | Worth a look | Looks good`; only P0/P1 count as changes requested), a
summary, a description-vs-changes check, inline comments (max 25, with `suggestion` blocks for one-line fixes or deletions),
and the model in the footer. Each finding has a severity (P0 must not merge, P1 serious, P2 normal, P3 low, nit trivial),
a confidence and the perspective that found it. P3 and nit findings below 0.7 confidence are dropped. Fixes must be
minimal, keep the build and tests green, and never add abstractions, dependencies or refactors. Comments added in the diff
that restate code, tell history or read like notes to an AI are flagged. It always writes in English.

The job log shows what the agent does as it happens: every file it reads or searches, every provider retry (for example
HTTP 429 "Too much concurrency"), and per call the turns, tool calls and tokens.

### Approval and the gate

The review is a `COMMENT`, except when nothing is left (no finding, none of its own threads unresolved, no errors, no time
budget hit): then it is an `APPROVE`. It never requests changes. Approving needs the repository setting *Settings >
Actions > General > Allow GitHub Actions to create and approve pull requests*; without it the review falls back to a
comment and the log says so.

The `gate` check lists the bot's unresolved threads and fails while any is open. GitHub has no event for resolving a
thread, so after resolving threads re-run the `gate` job or add the `ai-review` label.

### Time budget and large PRs

A review finishes within `time_budget` (15 minutes). Two minutes before the end the agent is told to stop exploring and
return what it has verified; at the end it is stopped. Only one review per machine uses the model at a time (a lock in
`run.sh`); a review waiting for the lock starts its budget once it gets it.

The diff goes into the prompt up to `batch_chars` (120k characters): code first, then tests, docs and styles, then data.
Lockfiles, binaries and minified files are skipped; data files are cut at 30k characters, and when one folder has 10 or
more changed data files of one type only 2 go into the diff and the rest are listed for the agent to open.

- **Detailed review:** a diff that fits is reviewed line by line, all severities.
- **Broad review of a large PR:** otherwise the agent gets code and tests only. Each file keeps a fair share of its diff;
  when that share would be too small, every file is shown as an outline (its top-level added lines: declarations,
  exports, routes). Docs, styles and data are listed. The agent looks at architecture, contracts, migrations,
  security-sensitive paths and error handling, opens the riskiest files itself (at most 5), and reports P0-P2 only.

### Review modes

The mode is picked from the PR's head branch (`release_from` input, default `staging`):

| PR | Mode | Focus |
|---|---|---|
| feature branch to `main` or `staging` | feature | one change in depth |
| `staging` to `main` | release | combined effect of already-reviewed features: interactions, migrations, config, deploy and rollback, client API breaks; P0-P2 only |

## Requirements

- A Linux machine (x64 or arm64) with systemd, `sudo`, `git`, `curl`, `python3` (3.9+) and [`gh`](https://cli.github.com)
  logged in as an admin of the repository. It only makes outgoing connections; no inbound port is needed.
- A model: either an API key for a provider that Pi supports (`pi --list-models` after install), or a local server with an
  OpenAI-compatible API (see [Local models](#local-models)). The default model is `zai/glm-5.3-flash`, which needs a Z.ai
  key; set the `model` input to use another one.

## Install on a Linux machine

```sh
git clone https://github.com/tyfnacici/ai-review.git && cd ai-review
./install.sh <owner>/<repo>          # or ./install.sh <org> for every repository of an organization
```

The script creates a system user `ai-review` (home `/opt/ai-review`, no sudo), installs the latest Pi (checksum
verified), clones this repository, and registers and starts a runner with the label `ai-review` as a systemd service.
It ends by printing the label to use in the calling workflow.

Then put the API key of your model provider into `/opt/ai-review/.config/ai-review/env` (mode 600; it never goes to
GitHub):

```sh
sudo -u ai-review nano /opt/ai-review/.config/ai-review/env
# ZAI_API_KEY=...          for zai/... models
# ANTHROPIC_API_KEY=...    for anthropic/... models
```

`pi --help` lists every provider and its variable.

| Variable | Default | Meaning |
|---|---|---|
| `LABEL` | `ai-review` | Runner label; the workflow's `runs_on` must match it |
| `RUNNER_NAME` | `<hostname>-<repo>` | Runner name; give each runner on a machine its own |
| `KEY_FILE` | | An env file to install as the key file |
| `AI_REVIEW_URL` | this repository | Clone URL of the reviewer code (your fork) |
| `RUNNER_USER` / `RUNNER_HOME` | `ai-review` / `/opt/ai-review` | System user and its home |

Run the script again for another repository; the user, Pi and key are shared. To review several PRs at once, add runners
on the same machine (`RUNNER_NAME=<name>-2 ./install.sh <owner>/<repo>`): each takes the next PR, checks it out and waits
for the model lock.

**Updates.** Pi is updated to its latest release at the start of every review job (the previous version stays in
`~/pi.old`), the runner updates itself, and the reviewer code is synced from `main` before every job. gitleaks,
osv-scanner and the GitHub actions are pinned to checked versions on purpose.

**Remove a runner.**

```sh
cd /opt/ai-review/runners/<runner name>
sudo ./svc.sh stop && sudo ./svc.sh uninstall
sudo -u ai-review ./config.sh remove --token "$(gh api -X POST repos/<owner>/<repo>/actions/runners/remove-token --jq .token)"
```

## Add it to a project

1. Install a runner for the repository (above).
2. Add `.github/workflows/ai-review.yml` to the project:
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
     vulnerability-alerts: read   # lets the review mention open Dependabot alerts
   jobs:
     review:
       uses: tyfnacici/ai-review/.github/workflows/review.yml@main
       with:
         runs_on: '["self-hosted", "ai-review"]'
         model: zai/glm-5.3-flash
   ```
3. Create the label once: `gh label create ai-review -R <owner>/<repo> --description "Run the AI review again"`.
4. Enable *Settings > Actions > General > Allow GitHub Actions to create and approve pull requests* so the bot can approve.
5. Keep the project's `CLAUDE.md` up to date; it is the review rulebook (what the project is, how it is built and tested,
   conventions, files that must not change).
6. Turn on Dependabot alerts for the repository (alerts only; automatic PRs are not needed).
7. To make the merge button wait for the gate: in the branch protection of `main` require the `review / gate` status check
   and turn on *Require conversation resolution before merging*. GitHub offers branch protection for private repositories
   only on a paid plan (Pro, Team) and for public repositories for free. Without it the gate is red or green on the PR,
   but nothing blocks the merge.

## Settings (workflow inputs)

| Input | Default | Meaning |
|---|---|---|
| `runs_on` | `["self-hosted", "ai-review"]` | Runner labels (JSON list) |
| `model` | `zai/glm-5.3-flash` | Pi model spec (`provider/model`); `pi --list-models` lists them, e.g. `zai/glm-5.3` for deeper and slower reviews, or `local/<id>` for a [local model](#local-models) |
| `thinking` | `medium` | Pi thinking level (`off` ... `max`); lower is faster |
| `parallel` | `1` | Follow-up calls at once for a follow-up larger than one batch; keep 1 on plans with low concurrency |
| `batch_chars` | `120000` | Diff characters per review call; larger diffs are split into batches |
| `time_budget` | `15` | Minutes for the whole review; `0` = no limit |
| `release_from` | `staging` | Head branch that gets a release review |
| `trigger_label` | `ai-review` | Label that runs the review again |

## Local models

Pi talks to any server with an OpenAI-compatible API, so the review can run on a model on your own hardware and no code
leaves your network. The reviewer needs a model that supports tool calling and follows instructions well; small models
find fewer bugs and add more noise than a hosted one.

1. Run the server (Ollama, `llama serve`, LM Studio, vLLM, ...) so the runner machine can reach it, with a context window
   of at least 32k tokens.
2. Describe it for Pi in `/opt/ai-review/.pi-agent/models.json` (as the runner user, so the file belongs to it):
   ```json
   {
     "providers": {
       "local": {
         "baseUrl": "http://localhost:11434/v1",
         "api": "openai-completions",
         "apiKey": "local",
         "models": [{ "id": "qwen2.5-coder:7b", "contextWindow": 32768, "maxTokens": 4096 }]
       }
     }
   }
   ```
   `id` is the model name the server expects, `contextWindow` must match what the server was started with, and `apiKey` is
   any placeholder unless the server checks one (then use `"$NAME"` to read it from the key file). Check it with
   `sudo -u ai-review env PI_CODING_AGENT_DIR=/opt/ai-review/.pi-agent /opt/ai-review/pi/pi --list-models local`.
3. Select it in the calling workflow and size the diff to the context window:
   ```yaml
   with:
     model: local/qwen2.5-coder:7b
     batch_chars: 48000        # about 1.5 characters per context token (32k tokens)
     time_budget: 30           # local models are slower
     thinking: "off"           # for models that do not reason
   ```

A diff larger than `batch_chars` gets the broad review, so a small context window means more broad reviews. The model lock
in `run.sh` already makes one review at a time use the model, which suits a single GPU. See the Pi documentation
(`docs/models.md`, `docs/llama-cpp.md`) for more server and provider options.

## Security model

- **Your key stays on your machine.** It lives in a file of the runner user (mode 600) and is never given to GitHub. Only
  the model provider you configure receives code.
- **No fork PRs, no drafts.** Their jobs are skipped, so untrusted code never runs on the runner.
- **The agent can only read.** Pi runs with `read`, `grep`, `find` and `ls`, offline mode, and no session storage. PR
  title, description, comments and diff are treated as untrusted data; instructions inside them are not followed.
- **The rulebook comes from the base branch**, so a PR cannot weaken its own review.
- **Token permissions** are the caller's: `pull-requests: write` for the review, `contents: write` only so the bot can
  resolve its own threads, `vulnerability-alerts: read` for the Dependabot line. Checkouts do not persist credentials.
- **Pinned tools.** gitleaks and osv-scanner are downloaded at a fixed version and verified by SHA-256; the actions are
  pinned by commit. Pi follows its latest release, verified against that release's checksum file.
- The runner user has no sudo. A job runs repository code only through the checkout; nothing in a PR is executed.

## Limitations

- A model can be wrong or miss things. Treat the review as a second pair of eyes, not as a gate on its own.
- The first review of a very large PR is broad, not line by line.
- The merge button is only blocked with branch protection (see step 7 above).
- A bot approval is not dismissed by later pushes (that is a branch protection feature); only the label starts a new review.
- Findings that are not tied to a changed line cannot be resolved as threads. They do not turn the gate red, but they prevent
  the approval.
- One review at a time per machine uses the model; others wait for the lock.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| The review job never starts | No online runner has the label in `runs_on`; check *Settings > Actions > Runners* and `./install.sh` output |
| Log shows `provider error, retry ... 429` | The plan's concurrency limit; keep `parallel` at 1 and one review at a time |
| `Dependabot alerts not readable` in the log | The calling workflow lacks `vulnerability-alerts: read`, or Dependabot alerts are off; the line is just left out |
| `could not approve` in the log | *Allow GitHub Actions to create and approve pull requests* is off; the review was posted as a comment |
| `gate` stays red after resolving threads | Re-run the `gate` job or add the `ai-review` label |
| Review ends with "The review reached its time budget" | Large PR or slow model; raise `time_budget` or use a faster model |
| Provider or key errors | Check the key file of the runner user and `pi --list-models` for the model name |
| A local model does not answer or loops | The model lacks tool calling, the context window is smaller than `contextWindow` says, or `batch_chars` is too large for it |

## Files

| File | Purpose |
|---|---|
| `review.py` | Orchestration: PR data, the review or follow-up agent run (Pi RPC mode), posting, thread resolution; `--gate` only lists the unresolved threads |
| `prompts/review.md` | Reviewer instructions and output contract |
| `prompts/followup.md` | Follow-up review instructions |
| `vendor/code-review-skill/` | Checklists from [awesome-skills/code-review-skill](https://github.com/awesome-skills/code-review-skill) (MIT, pinned commit in `SOURCE.md`) |
| `run.sh` | Runner entrypoint: loads the key file, takes the model lock and calls `review.py` |
| `deps.py` | Lockfile check of the `dependencies` job: new known vulnerabilities in a PR compared with its base |
| `update-pi.sh` | Installs or updates Pi to its latest release; runs at the start of every review job |
| `install.sh` | Sets up a self-hosted runner for ai-review on a Linux machine |
| `.github/workflows/review.yml` | Reusable workflow (`secrets`, `dependencies`, `review`, `gate`) called by projects |

## Development

The review can be tried on any PR without posting anything. From a checkout at the PR head, with `gh` logged in, `pi`
installed and the provider key in the environment:

```sh
python3 review.py --repo <owner>/<repo> --pr <number> --workdir . --dry-run
```

This prints the review as JSON instead of posting it. `python3 review.py --gate --repo <owner>/<repo> --pr <number>
--workdir .` lists a PR's unresolved bot threads, and `python3 deps.py --osv <osv-scanner> --base origin/main` checks the
lockfiles changed in the current checkout. The reviewer is plain Python 3 with no dependencies; see `CLAUDE.md` for the
conventions.

## Credits

[Pi](https://github.com/badlogic/pi-mono) (the agent), [gitleaks](https://github.com/gitleaks/gitleaks),
[osv-scanner](https://github.com/google/osv-scanner), and the checklists from
[awesome-skills/code-review-skill](https://github.com/awesome-skills/code-review-skill).
