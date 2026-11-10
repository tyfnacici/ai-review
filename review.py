#!/usr/bin/env python3
"""AI PR review: review a GitHub PR with the Pi coding agent and post the result as one PR review.

One agent reviews the PR from three perspectives (correctness & contracts, security & performance,
simplicity & scope), reads code and checklists from the vendored awesome-skills/code-review-skill as it
needs them, verifies its own findings and writes the review. Every later push gets one short follow-up
call instead: earlier findings are re-checked (fixed ones get a reply and their thread is resolved) and
only serious problems in the new changes are added, so a PR converges. The diff comes from the local
checkout, so PRs of any size work; a PR larger than one batch gets a broad review: code and tests only,
each file's diff cut to a fair share or shown as an outline, P0-P2 only.

Usage: review.py --repo owner/name --pr N --workdir <checkout at PR head>
                 [--mode feature|release] [--model zai/glm-5.3-flash] [--thinking medium] [--parallel 1]
                 [--batch-chars 120000] [--time-budget 15] [--pi pi] [--dry-run]
The time budget (minutes) caps the whole review: two minutes before the end the agent is told to stop
exploring and answer, at the end it is stopped. Pi runs in RPC mode, so the job log shows every tool call,
provider retry (e.g. HTTP 429) and the tokens used.
Feature mode reviews one change in depth; release mode (e.g. staging -> main) looks at the combined effect.
Needs: gh (authenticated via GH_TOKEN), pi, and the model provider's API key in the environment (e.g. ZAI_API_KEY).
Project rules come from CLAUDE.md / AGENTS.md / .github/ai-review.md on the BASE branch.
"""
import argparse
import base64
import itertools
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
GUIDES = os.path.join(HERE, "vendor", "code-review-skill", "reference")
MARKER = "ai-review:v2"
MARKER_RE = r"<!-- (?:ai-review:v2|muesli:v1) head=(\w+) findings=([A-Za-z0-9+/=]*) -->"  # v1 = older reviews
CONTEXT_FILES = ["CLAUDE.md", "AGENTS.md", ".github/ai-review.md"]
DATA_CHARS = 30_000  # a data file's diff is cut here; code diffs are never cut, only split into parts
LOCKFILES = {"package-lock.json", "pnpm-lock.yaml", "yarn.lock", "bun.lockb", "Cargo.lock", "Gemfile.lock",
             "poetry.lock", "uv.lock", "composer.lock", "go.sum"}
DATA_EXTS = {".json", ".csv", ".tsv", ".xml", ".svg", ".txt", ".geojson"}
DOC_EXTS = {".md", ".mdx", ".rst", ".css", ".scss", ".less"}
BULK_MIN, BULK_SAMPLE = 10, 2  # a folder with >= 10 changed data files of one type: 2 go in the diff, the rest are listed
MAX_INLINE = 25
CALL_TIMEOUT = 1800  # seconds per Pi call when there is no time budget
STEER_LEAD = 120  # seconds before the deadline the agent is told to answer
STEER = "Time is up. Do not call any more tools. Return the JSON object now with the findings you have verified."
BROAD_MIN_SHARE = 3000  # below this share per file, a broad review shows outlines instead of cut diffs
BROAD_NOTE = (
    "LARGE PR, BROAD REVIEW: this PR is too large to read line by line in the time available. The diff below "
    "has code and tests only, each file's diff is cut short or shown as an outline (its top-level added lines), "
    "and the other files are only listed. Look at the "
    "change broadly: architecture and layering, contracts between the changed parts (API, "
    "data, schema, migrations), security-sensitive paths, error-handling patterns, risky dependencies and "
    "config. Open at most 5 files, the ones that look riskiest. Skip local style and minor issues; report "
    "P0-P2 only.\n\n")
SEVERITIES = ["P0", "P1", "P2", "P3", "nit"]
PRIOR_STATUS_LABEL = {"resolved": "fixed", "still_open": "still open", "deferred": "deferred",
                      "obsolete": "no longer applies", "rejected": "not a defect"}
MIN_MINOR_CONFIDENCE = 0.7  # P3/nit below this are dropped

PERSPECTIVES = {
    "correctness": (
        "Correctness, contracts and tests: logic errors, edge cases, off-by-one, null/empty handling, error "
        "handling, panics/exceptions, state and lifecycle, concurrency and locking; API/wire compatibility, "
        "data/schema/migration safety, callers that break, config. Tests: which existing tests break (name them) "
        "and what the change leaves untested.",
        ["common-bugs-checklist.md", "cross-cutting/error-handling-principles.md",
         "cross-cutting/async-concurrency-patterns.md", "code-review-best-practices.md"]),
    "security_performance": (
        "Security and performance. Security: authentication and authorization, input validation, injection "
        "(SQL, command, XSS), secrets, unsafe defaults, abuse paths (rate limits, enumeration), data exposure. "
        "Performance: algorithmic complexity, N+1 queries, unbounded growth, blocking calls in async code, leaks.",
        ["security-review-guide.md", "cross-cutting/sql-injection-prevention.md", "cross-cutting/xss-prevention.md",
         "performance-review-guide.md", "cross-cutting/n-plus-one-queries.md"]),
    "simplicity": (
        "Simplicity and scope (KISS). Be strict; this team values simple, minimal changes that follow the "
        "project's existing structure. Use the PR description and the project context (CLAUDE.md) as the "
        "reference for intended scope. Flag: files or changes outside the stated purpose; unnecessary "
        "churn (renames, reformatting, rewrites nobody asked for); over-engineering (new abstractions, "
        "layers, options, generics or config for a single use); more code than the problem needs; "
        "duplicating an existing helper instead of reusing it; not following existing patterns, naming "
        "or folder layout; new dependencies that are not clearly necessary; dead code. For each, say "
        "what the simpler alternative is. Also check whether the description matches what the code does. "
        "Comments: this team wants almost no comments, because later AI tools read them as instructions. Flag every "
        "comment ADDED in the diff that restates what the code does, describes change history ('fixed X', "
        "'changed from Y'), leaks chat or task context ('as discussed', 'per user request', 'the user wants'), "
        "or reads like a note or instruction to an AI ('TODO: AI', 'next model should'). Severity P2 for leaked "
        "context or AI instructions, P3 for merely redundant comments; set `delete_line: true` when the fix is to "
        "remove that single line. Keep only short comments that explain a non-obvious constraint or pitfall, "
        "license headers, and doc comments where they are the project's convention; follow CLAUDE.md if it says "
        "otherwise.",
        ["code-quality-universal.md"]),
}
RELEASE_NOTE = (
    "RELEASE REVIEW: this PR promotes features that were already reviewed one by one (e.g. staging -> main). "
    "Do not re-review individual lines in depth. Look at the combined effect: interactions "
    "between the features, migrations and data, config/env changes, deploy and rollback risk, breaking API "
    "changes for clients (web, mobile), leftover debug code or half-finished work. Report P0-P2 only.\n\n")
LANGUAGE_GUIDES = {".rs": ["rust.md"], ".ts": ["typescript.md"], ".tsx": ["typescript.md", "react.md"],
                   ".jsx": ["react.md"], ".py": ["python.md"], ".kt": ["kotlin.md"], ".php": ["php.md"],
                   ".vue": ["vue.md"], ".go": ["go.md"], ".java": ["java.md"], ".swift": ["swift.md"],
                   ".dart": ["dart.md"], ".css": ["css-less-sass.md"], ".scss": ["css-less-sass.md"]}


def log(msg):
    """Progress line for the Actions job log."""
    print(f"{time.strftime('%H:%M:%S')} {msg}", file=sys.stderr, flush=True)


def tracked(total):
    """Wraps a call so each start and finish is logged with how many are done."""
    done = itertools.count(1)

    def run(where, fn):
        log(f"{where}: started")
        start, ok = time.time(), False
        try:
            out = fn()
            ok = True
            return out
        finally:
            log(f"{where}: {'done' if ok else 'failed'} in {time.time() - start:.0f}s ({next(done)}/{total})")
    return run


def gh(*args, input_text=None, check=True):
    out = subprocess.run(["gh", *args], input=input_text, capture_output=True, text=True)
    if check and out.returncode != 0:
        sys.exit(f"gh {' '.join(args[:3])} failed: {out.stderr.strip()}")
    return out


def dependabot_summary(repo, changed):
    """One line on the repository's open Dependabot alerts; empty when they cannot be read (no permission, disabled)."""
    out = gh("api", f"repos/{repo}/dependabot/alerts?state=open&per_page=100", "--paginate", "-q",
             r'.[] | [.security_advisory.severity, .dependency.manifest_path, .dependency.package.name] | @tsv',
             check=False)
    if out.returncode != 0:
        log("Dependabot alerts not readable (the caller needs `vulnerability-alerts: read`); skipped")
        return ""
    alerts = [row.split("\t") for row in out.stdout.splitlines() if row.count("\t") == 2]
    if not alerts:
        return ""
    counts = ", ".join(f"{sum(s == sev for s, _, _ in alerts)} {sev}" for sev in ("critical", "high", "medium", "low")
                       if any(s == sev for s, _, _ in alerts))
    line = f"**Dependabot:** {len(alerts)} open alert(s) on the repository ({counts})."
    touched = sorted({m for _, m, _ in alerts if m in changed})
    for manifest in touched:
        names = sorted({n for _, m, n in alerts if m == manifest})
        line += (f" This PR changes `{manifest}`, which has {sum(m == manifest for _, m, _ in alerts)} of them "
                 f"({', '.join(names[:5])}{', ...' if len(names) > 5 else ''}).")
    return line


def base_context(repo, base_ref):
    parts = []
    for path in CONTEXT_FILES:
        out = gh("api", f"repos/{repo}/contents/{path}?ref={base_ref}",
                 "-H", "Accept: application/vnd.github.raw", check=False)
        if out.returncode == 0 and out.stdout.strip():
            parts.append(f"# Project context: {path} (from `{base_ref}`)\n\n{out.stdout.strip()}")
    return "\n\n".join(parts)


def git(workdir, *args):
    out = subprocess.run(["git", "-C", workdir, *args], capture_output=True, text=True, errors="replace")
    if out.returncode != 0:
        sys.exit(f"git {' '.join(args[:2])} failed: {out.stderr.strip()}")
    return out.stdout


def local_diff(workdir, base, head):
    """Diff from the checkout; GitHub's diff API refuses PRs with more than 300 files."""
    return git(workdir, "diff", "--no-color", "--no-ext-diff", "--no-textconv", base, head)


def split_files(diff):
    """[(path, that file's diff)] in diff order."""
    out = []
    for chunk in re.split(r"(?m)^(?=diff --git )", diff):
        if chunk.startswith("diff --git "):
            m = re.search(r"(?m)^\+\+\+ b/(.+)$", chunk) or re.match(r"diff --git a/.* b/(.+)", chunk)
            out.append((m.group(1), chunk))
    return out


def priority(path):
    """Review order: code, then tests, then docs and styles, then data."""
    ext = os.path.splitext(path)[1].lower()
    if ext in DATA_EXTS:
        return 3
    if ext in DOC_EXTS:
        return 2
    return 1 if re.search(r"(^|/)(tests?|__tests__|spec)/|[._](test|spec)\.", path) else 0


def plan_batches(file_diffs, limit, max_batches=0):
    """Pack file diffs into batches of about `limit` characters, most important files first. Returns (batches,
    skipped, listed): lockfiles, binaries and minified files are skipped; in a folder with many changed data files
    only a sample goes in and the rest are listed. With max_batches (broad review of a large PR) only code and
    tests go in, each cut to a fair share of the space; the other files are listed."""
    kind = lambda p: (os.path.dirname(p), os.path.splitext(p)[1].lower())
    bulk = Counter(kind(p) for p, _ in file_diffs if kind(p)[1] in DATA_EXTS)
    seen, kept, skipped, listed = Counter(), [], [], []
    for path, text in sorted(file_diffs, key=lambda d: priority(d[0])):
        if (os.path.basename(path) in LOCKFILES or path.endswith((".min.js", ".min.css", ".map"))
                or re.search(r"(?m)^Binary files ", text)):
            skipped.append(path)
            continue
        if bulk[kind(path)] >= BULK_MIN:
            seen[kind(path)] += 1
            if seen[kind(path)] > BULK_SAMPLE:
                listed.append(path)
                continue
        if max_batches and priority(path) >= 2:
            listed.append(path)
            continue
        if kind(path)[1] in DATA_EXTS and len(text) > DATA_CHARS:
            text = text[:DATA_CHARS] + f"\n[diff of {path} cut at {DATA_CHARS} characters; read the file for the rest]\n"
        kept.append((path, text))
    if max_batches:
        kept = shrink(kept, max_batches * limit * 9 // 10)
    batches, size = [[]], 0
    for path, text in kept:
        for part in split_large(text, limit):
            if batches[-1] and size + len(part) > limit:
                if len(batches) == max_batches:
                    listed.append(path)
                    break
                batches.append([])
                size = 0
            batches[-1].append((path, part))
            size += len(part)
    return [b for b in batches if b], skipped, listed


def shrink(file_diffs, space):
    """Fit file diffs into about `space` characters for a broad review: each keeps a fair share of its diff, or of
    its outline when that share would be too small to be useful."""
    space -= 150 * len(file_diffs)  # the notes added below
    cap = fair_share([len(t) for _, t in file_diffs], space)
    if cap >= BROAD_MIN_SHARE:
        return [(p, t if len(t) <= cap else
                 f"{t[:cap]}\n[diff of {p} cut at {cap} characters for a broad review; read the file for the rest]\n")
                for p, t in file_diffs]
    outlines = [(p, outline(t)) for p, t in file_diffs]
    cap = fair_share([len(t) for _, t in outlines], space)
    return [(p, f"{t[:cap]}\n[outline of {p}'s diff: top-level added lines only"
             f"{f', cut at {cap} characters' if len(t) > cap else ''}; read the file for the rest]\n")
            for p, t in outlines]


def outline(diff):
    """A file diff reduced to its headers and the added lines that are not indented: declarations, exports, routes."""
    keep = [l for l in diff.splitlines() if l.startswith(("diff ", "--- ", "+++ ", "@@"))
            or (l.startswith("+") and l[1:2].strip() and l[1:].strip() not in ("}", "};", "})", "});", ")", "]", "];"))]
    return "\n".join(keep) + "\n"


def fair_share(sizes, space):
    """Largest per-file cap c with sum(min(size, c)) <= space."""
    rest = len(sizes)
    for size in sorted(sizes):
        if size * rest > space:
            return space // rest
        space -= size
        rest -= 1
    return max(sizes, default=0)


def split_large(text, limit):
    """A file diff larger than one batch, split by lines into parts that each repeat the file header."""
    if len(text) <= limit:
        return [text]
    lines = text.splitlines(keepends=True)
    head = next((i for i, l in enumerate(lines) if l.startswith("@@")), 0)
    header, parts, cur = "".join(lines[:head]), [], ""
    for line in lines[head:]:
        if cur and len(header) + len(cur) + len(line) > limit:
            parts.append(cur)
            cur = ""
        cur += line
    parts.append(cur)
    return [f"{header}[part {k} of {len(parts)} of this file's diff]\n{c}" for k, c in enumerate(parts, 1)]


def perspectives_prompt(paths):
    """The three review perspectives, each with the checklists the agent may read when they help it decide."""
    exts = {os.path.splitext(p)[1] for p in paths}
    text = ["# Three perspectives\n\nReview the PR from all three. Checklists are listed as files you may read with "
            "the read tool when one would help you decide; do not read them all."]
    for name, (focus, guides) in PERSPECTIVES.items():
        if name == "correctness":
            guides = guides + sorted({g for e in exts for g in LANGUAGE_GUIDES.get(e, [])})
        text.append(f"## {name}\n\n{focus}\n\nChecklists: " + ", ".join(os.path.join(GUIDES, g) for g in guides))
    return "\n\n".join(text)


def commentable_lines(diff):
    """Map path -> {new-file line: is_added} for lines GitHub accepts as RIGHT-side anchors."""
    lines, path, new_line = {}, None, 0
    for row in diff.splitlines():
        if row.startswith("+++ "):
            path = row[6:] if row.startswith("+++ b/") else None
            continue
        if row.startswith("@@"):
            m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)", row)
            new_line = int(m.group(1)) if m else 0
            continue
        if path is None or row.startswith(("--- ", "diff ", "index ")):
            continue
        if row.startswith("+") or row.startswith(" "):
            lines.setdefault(path, {})[new_line] = row.startswith("+")
            new_line += 1
    return lines


def previous_review(reviews):
    """Latest bot review's findings (from its hidden marker), or None."""
    for r in reversed(reviews):
        m = re.search(MARKER_RE, r.get("body") or "")
        if m:
            try:
                findings = json.loads(base64.b64decode(m.group(2)) or b"[]")
            except ValueError:
                findings = []
            for i, f in enumerate(findings, 1):
                f["id"] = f"p{i}"
            return {"head": m.group(1), "findings": findings}
    return None


def delta_since(workdir, prior, head):
    """(added lines per path, diff text) since the previous bot review, or None when a full review is due."""
    if not prior:
        return None
    if prior["head"] == head:
        return {}, ""
    ancestor = subprocess.run(["git", "-C", workdir, "merge-base", "--is-ancestor", prior["head"], head],
                              capture_output=True)
    if ancestor.returncode != 0:
        return None  # force-push or rebase: anchors are unreliable, review fresh
    diff = local_diff(workdir, prior["head"], head)
    return {p: {n for n, added in lines.items() if added} for p, lines in commentable_lines(diff).items()}, diff


def batch_note(batches, i, skipped, sampled):
    """What this call's diff covers, for prompts of a PR split into batches or with files left out."""
    notes = []
    if len(batches) > 1:
        notes.append(f"This PR is too large for one pass, so its diff is split into {len(batches)} batches. "
                     f"This is batch {i + 1} of {len(batches)}: review the files in the diff below; the full "
                     "file list is context.")
    if sampled:
        notes.append("Files left out of the diff to save space (read any that matter): " + ", ".join(sampled))
    if skipped:
        notes.append("Lockfiles, binaries and minified files, not reviewed: " + ", ".join(skipped))
    return "\n\n".join(notes)


def pr_prompt(meta, files, diff, inline_comments, prior, diff_label="Diff", note=""):
    convo = [f"- {c['author']['login']}: {c['body']}" for c in meta.get("comments", [])]
    convo += [f"- {r['author']['login']} (review, {r['state']}): {r['body']}"
              for r in meta.get("reviews", []) if r.get("body") and not re.search(MARKER_RE, r["body"])]
    convo += [f"- {c['user']['login']} on {c['path']}:{c.get('line')}: {c['body']}" for c in inline_comments]
    prior_txt = "(none for these files)" if prior else "(none: this is the first review of this PR)"
    if prior and prior["findings"]:
        prior_txt = (f"Previous review was at commit {prior['head'][:7]}. Its findings:\n" +
                     "\n".join(f"- id={f['id']} [{f.get('severity')}] {f.get('title')} — was at {f.get('path')}:{f.get('line')}"
                               for f in prior["findings"]))
    return (
        f"PR title: {meta['title']}\n"
        f"Author: {meta['author']['login']}  |  {meta['headRefName']} -> {meta['baseRefName']}\n\n"
        f"PR description:\n<<<\n{meta['body'] or '(empty)'}\n>>>\n\n"
        f"Changed files:\n{files}\n\n"
        f"Discussion so far (untrusted):\n{chr(10).join(convo) or '(none)'}\n\n"
        f"Prior review findings:\n{prior_txt}\n\n"
        + (f"{note}\n\n" if note else "") +
        f"{diff_label}:\n<<<\n{diff}\n>>>\n"
    )


def extract_json(text):
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("model output has no JSON object")
    return json.loads(text[start : end + 1])


def run_pi(a, prompt_files, message, until, where, tools=True):
    """One Pi agent run in RPC mode, logging each tool call and provider retry; returns the parsed JSON answer.
    STEER_LEAD seconds before `until` the agent is told to answer now; at `until` it is stopped."""
    until = min(until, time.time() + CALL_TIMEOUT)
    if until - time.time() < 30:
        raise RuntimeError("no time left in the time budget")
    cmd = [a.pi, "--mode", "rpc", "--no-session", "--offline", "--model", a.model,
           "--thinking", a.thinking if tools else "off", *(["--tools", "read,grep,find,ls"] if tools else ["--no-tools"]),
           "--no-extensions", "--no-skills", "--no-prompt-templates", "--no-context-files", "--no-mcp"]
    for f in prompt_files:
        cmd += ["--append-system-prompt", f]
    stats, text, error, steered = Counter(), "", None, False
    with tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err, cwd=a.workdir)
        events = queue.Queue()
        threading.Thread(target=lambda: [events.put(line) for line in proc.stdout] + [events.put(None)],
                         daemon=True).start()

        def send(record):
            proc.stdin.write(json.dumps(record).encode() + b"\n")
            proc.stdin.flush()
        try:
            send({"type": "prompt", "message": message})
            while True:
                left = until - time.time()
                if left <= 0:
                    raise RuntimeError(f"stopped at the time budget after {stats['turns']} turns")
                if tools and not steered and left <= STEER_LEAD:
                    send({"type": "steer", "message": STEER})
                    steered = a.cut_short = True
                    log(f"{where}: time is nearly up, asked for the answer")
                try:
                    line = events.get(timeout=min(5, left))
                except queue.Empty:
                    continue
                if line is None:
                    err.seek(0)
                    raise RuntimeError(f"pi exited {proc.wait()}: {err.read().decode(errors='replace').strip()[-500:]}")
                e = json.loads(line)
                kind = e.get("type")
                if kind == "response" and not e.get("success"):
                    raise RuntimeError(f"pi refused {e.get('command')}: {e.get('error')}")
                if kind == "turn_end":
                    stats["turns"] += 1
                elif kind == "tool_execution_start":
                    stats["tools"] += 1
                    args = e.get("args") or {}
                    log(f"{where}: {e.get('toolName')} {args.get('path') or args.get('pattern') or ''}".rstrip())
                elif kind == "auto_retry_start":
                    stats["retries"] += 1
                    log(f"{where}: provider error, retry {e.get('attempt')} in {e.get('delayMs', 0) / 1000:.0f}s: "
                        f"{(e.get('errorMessage') or '')[:160]}")
                elif kind == "message_end" and e["message"].get("role") == "assistant":
                    m = e["message"]
                    stats["tokens"] += (m.get("usage") or {}).get("input", 0)
                    error = m.get("errorMessage") if m.get("stopReason") == "error" else None
                    text = "".join(c.get("text", "") for c in m.get("content", []) if c.get("type") == "text") or text
                elif kind == "agent_settled":
                    break
        finally:
            proc.kill()
            proc.wait()
    if tools:
        log(f"{where}: {stats['turns']} turns, {stats['tools']} tool calls, {stats['retries']} provider retries, "
            f"{stats['tokens'] // 1000}k input tokens")
    if error and not text:
        raise RuntimeError(error)
    try:
        return extract_json(text)
    except ValueError:  # usually one unescaped quote; a cheap no-tools pass repairs it
        if not tools:
            raise
        return run_pi(a, [], "Repair this into valid JSON with the same content. Return only the JSON.\n\n" + text,
                      max(until, time.time() + 120), where, tools=False)


def write_temp(text):
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as fh:
        fh.write(text)
    return fh.name


def verdict_of(findings):
    """Host-owned verdict: only P0/P1 block; the model's own opinion is advisory."""
    if any(f.get("severity") in ("P0", "P1") for f in findings):
        return "Changes requested"
    if any(f.get("severity") in ("P2", "P3") for f in findings):
        return "Worth a look"
    return "Looks good"


def finding_md(f, with_location):
    loc = f"`{f.get('path') or 'repo-wide'}{':' + str(f['line']) if f.get('line') else ''}` " if with_location else ""
    conf = f" · confidence {f['confidence']:.1f}" if isinstance(f.get("confidence"), (int, float)) else ""
    lenses = f" · {', '.join(f['lenses'])}" if isinstance(f.get("lenses"), list) and f["lenses"] else ""
    return f"{f.get('severity')} {loc}**{f.get('title', '')}**{conf}{lenses}"


def inline_body(f, is_added_line):
    body = f"{finding_md(f, False)}\n\n{f.get('body', '').strip()}"
    suggestion = f.get("suggestion")
    if f.get("delete_line") is True and is_added_line:
        body += "\n\n```suggestion\n```"
    elif isinstance(suggestion, str) and suggestion.strip() and is_added_line:
        body += f"\n\n```suggestion\n{suggestion.rstrip()}\n```"
    return body


def render(result, findings, unanchored, prior_status, errors, cut_short, suppressed, mode, followup, model,
           elapsed, head_sha, calls, skipped, sampled, broad, alerts=""):
    kinds = [k for k, on in (("release review", mode == "release"), ("broad review of a large PR", broad),
                             ("follow-up", followup)) if on]
    kind = f" ({', '.join(kinds)})" if kinds else ""
    parts = [f"## Code review{kind}: {verdict_of(findings)}"]
    if followup and not findings:
        parts.append("**All previous findings are addressed and the new changes raise nothing new. No new comments.**")
    parts.append((result.get("summary") or "").strip())
    dc = result.get("description_consistency")
    if dc:
        parts.append(f"**Description vs. changes ({'consistent' if dc.get('ok') else 'inconsistent'}):** {(dc.get('notes') or '').strip()}")
    if (result.get("scope_notes") or "").strip():
        parts.append(f"**Simplicity & scope:** {result['scope_notes'].strip()}")
    if findings:
        counts = ", ".join(f"{sum(f.get('severity') == s for f in findings)}× {s}" for s in SEVERITIES
                           if any(f.get("severity") == s for f in findings))
        parts.append(f"**Findings:** {counts}")
    if prior_status:
        parts.append("**Earlier findings:**\n" + "\n".join(
            f"- {PRIOR_STATUS_LABEL.get(p.get('status'), p.get('status'))}: {p['title']}"
            f"{' — ' + p['note'] if p.get('note') else ''}" for p in prior_status))
    if unanchored:
        parts.append("**Other findings** (not tied to a changed line):\n" + "\n".join(
            f"- {finding_md(f, True)}: {f.get('body', '').strip()}" for f in unanchored))
    if (result.get("testing_notes") or "").strip():
        parts.append(f"**Testing:** {result['testing_notes'].strip()}")
    if (result.get("context_notes") or "").strip():
        parts.append(f"**Project rulebook (CLAUDE.md):** {result['context_notes'].strip()}")
    parts.append(alerts)
    if errors:
        parts.append("**Incomplete:** these review calls failed and their part was skipped: " + ", ".join(errors))
    if cut_short:
        parts.append("<sub>The review reached its time budget, so it was asked to finish with what it had verified.</sub>")
    if skipped or sampled:
        parts.append(f"<sub>Not reviewed line by line: {len(skipped)} lockfile/binary/minified file(s), "
                     f"{len(sampled)} file(s) only listed for the reviewer to open as needed.</sub>")
    if suppressed:
        parts.append(f"<sub>{suppressed} new minor finding(s) on previously reviewed code were dropped to avoid review loops.</sub>")
    carried = [{k: f.get(k) for k in ("severity", "title", "path", "line")} for f in findings]
    encoded = base64.b64encode(json.dumps(carried, ensure_ascii=False).encode()).decode()
    parts.append(f"<sub>{model} · Pi · {mode} mode · {calls} · {elapsed:.0f}s · {head_sha[:7]}</sub>\n"
                 f"<!-- {MARKER} head={head_sha} findings={encoded} -->")
    return "\n\n".join(p for p in parts if p)


THREADS_QUERY = """query($o:String!,$n:String!,$p:Int!){repository(owner:$o,name:$n){pullRequest(number:$p){
reviewThreads(first:100){nodes{id isResolved comments(first:1){nodes{databaseId body author{login}}}}}}}}"""
RESOLVE_MUTATION = "mutation($t:ID!){resolveReviewThread(input:{threadId:$t}){thread{isResolved}}}"
REPLY = {"resolved": "Fixed in {sha}.", "obsolete": "No longer applies as of {sha}: the code was removed or rewritten.",
         "rejected": "On re-check at {sha} this is not a defect.",
         "deferred": "Acknowledged as deliberately deferred; it will not be raised again."}


def open_bot_threads(repo, pr):
    """Unresolved inline threads started by the bot, as dicts with thread id, comment id and title."""
    owner, name = repo.split("/")
    data = json.loads(gh("api", "graphql", "-f", f"query={THREADS_QUERY}", "-F", f"o={owner}",
                         "-F", f"n={name}", "-F", f"p={pr}").stdout)
    threads = []
    for t in data["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]:
        first = (t["comments"]["nodes"] or [None])[0]
        if t["isResolved"] or not first or not (first["author"] or {}).get("login", "").startswith("github-actions"):
            continue
        title = re.search(r"\*\*(.+?)\*\*", first["body"])
        threads.append({"id": t["id"], "comment": first["databaseId"], "title": title.group(1) if title else None})
    return threads


def close_fixed_threads(repo, pr, prior_status, threads, head, dry_run):
    """Reply to and resolve the bot's own inline threads whose finding is no longer open."""
    closed = 0
    for p in prior_status:
        if p.get("status") not in REPLY:
            continue
        for t in [t for t in threads if t["title"] == p["title"]]:
            text = REPLY[p["status"]].format(sha=head[:7]) + (f" {p['note']}" if p.get("note") else "")
            if dry_run:
                print(f"would resolve thread {t['id']}: {text}")
            else:
                gh("api", "-X", "POST", f"repos/{repo}/pulls/{pr}/comments/{t['comment']}/replies",
                   "-f", f"body={text}")
                res = gh("api", "graphql", "-f", f"query={RESOLVE_MUTATION}", "-F", f"t={t['id']}", check=False)
                if res.returncode != 0:
                    print(f"warning: could not resolve thread {t['id']}: {res.stderr.strip()}", file=sys.stderr)
            threads.remove(t)
            closed += 1
    return closed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--pr", required=True, type=int)
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--model", default="zai/glm-5.3-flash")
    ap.add_argument("--thinking", default="medium")
    ap.add_argument("--parallel", type=int, default=1, help="follow-up calls at once")
    ap.add_argument("--batch-chars", type=int, default=120_000)
    ap.add_argument("--time-budget", type=float, default=15, help="minutes for the whole review; 0 = no limit")
    ap.add_argument("--mode", choices=["feature", "release"], default="feature")
    ap.add_argument("--pi", default="pi")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--gate", action="store_true", help="only report the bot's unresolved threads; exit 1 if any")
    a = ap.parse_args()
    if a.gate:
        open_threads = open_bot_threads(a.repo, a.pr)
        print(f"{len(open_threads)} unresolved AI review thread(s)")
        for t in open_threads:
            print(f"- {t['title']}")
        sys.exit(1 if open_threads else 0)
    budget = a.time_budget * 60 or float("inf")
    a.until = time.time() + budget - 30  # leaves time to post the review
    a.errors, a.cut_short = [], False

    meta = json.loads(gh("pr", "view", str(a.pr), "-R", a.repo, "--json",
                         "title,body,author,baseRefName,headRefName,comments,reviews").stdout)
    inline = json.loads(gh("api", f"repos/{a.repo}/pulls/{a.pr}/comments", "--paginate").stdout)
    raw_reviews = json.loads(gh("api", f"repos/{a.repo}/pulls/{a.pr}/reviews", "--paginate").stdout)
    head = git(a.workdir, "rev-parse", "HEAD").strip()
    base = git(a.workdir, "merge-base", f"origin/{meta['baseRefName']}", head).strip()
    files = "\n".join(f"- {p} (+{ad}/-{de})" for ad, de, p in
                      (row.split("\t", 2) for row in git(a.workdir, "diff", "--numstat", base, head).splitlines()))
    diff = local_diff(a.workdir, base, head)
    prior = previous_review(raw_reviews)
    rounds = sum(bool(re.search(MARKER_RE, r.get("body") or "")) for r in raw_reviews)
    # First review of a PR: full review. Every later push: a short follow-up on the delta.
    follow = delta_since(a.workdir, prior, head)
    followup = follow is not None
    delta, review_diff = follow if followup else (None, diff)
    # A PR whose batches do not fit in one wave of calls gets one broad pass instead of a detailed one.
    file_diffs = split_files(review_diff)
    wave = max(1, a.parallel) if followup else 1
    batches, skipped, sampled = plan_batches(file_diffs, a.batch_chars)
    broad = len(batches) > wave
    if broad:
        batches, skipped, sampled = plan_batches(file_diffs, a.batch_chars, wave)
    log(f"{'follow-up' if followup else 'full'} {a.mode} review{' (broad)' if broad else ''}: {len(batches)} "
        f"batch(es) of the diff, {len(skipped)} skipped and {len(sampled)} listed file(s), {a.model}, "
        f"thinking {a.thinking}, {a.time_budget:g}-minute budget")
    mode_note = (RELEASE_NOTE if a.mode == "release" else "") + (BROAD_NOTE if broad else "")
    label = "Changes since the previous review" if followup else "Diff"

    def batch_text(i, prior_part=prior):
        body = "".join(t for _, t in batches[i]) if batches else "(no new commits since the previous review)"
        return mode_note + pr_prompt(meta, files, body, inline, prior_part, label,
                                     batch_note(batches, i, skipped, sampled))

    context = base_context(a.repo, meta["baseRefName"])
    ctx_path = write_temp(context) if context else None

    alerts = "" if followup else dependabot_summary(
        a.repo, {row.split("\t", 2)[2] for row in git(a.workdir, "diff", "--numstat", base, head).splitlines()})
    started = time.time()
    try:
        if followup:
            result = followup_review(a, batches, ctx_path, batch_text, prior)
            calls = f"{max(1, len(batches))} follow-up call(s)"
        else:
            no_rules = "" if context else (f"\nProject context: none (no {', '.join(CONTEXT_FILES)} on "
                                           f"`{meta['baseRefName']}`).\n")
            result = full_review(a, batches, ctx_path, batch_text(0) + no_rules)
            calls = "1 review call"
    finally:
        if ctx_path:
            os.unlink(ctx_path)
    elapsed = time.time() - started

    # Earlier findings are matched by id, so the model cannot drift their titles. A prior finding the model
    # did not classify counts as still open, so nothing is silently dropped.
    prior_by_id = {f["id"]: f for f in (prior or {}).get("findings", [])}
    status_by_id = {p["id"]: p for p in result.get("prior_findings") or [] if p.get("id") in prior_by_id}
    prior_status = [dict(status_by_id.get(i, {"id": i, "status": "still_open"}), title=f["title"])
                    for i, f in prior_by_id.items()]
    allowed = SEVERITIES[:3] if a.mode == "release" or broad else SEVERITIES
    findings = [f for f in result.get("findings", []) if f.get("severity") in allowed
                and (f["severity"] in SEVERITIES[:3] or f.get("prior_id")
                     or (f.get("confidence") or 0) >= MIN_MINOR_CONFIDENCE)]
    for f in findings:
        if f.get("prior_id") in prior_by_id:
            f["title"] = prior_by_id[f["prior_id"]]["title"]
    listed = {f.get("prior_id") for f in findings}
    findings += [dict(prior_by_id[p["id"]], prior_id=p["id"], body=p.get("note", ""))
                 for p in prior_status if p["status"] == "still_open" and p["id"] not in listed]
    # Anti-loop rule: a follow-up only adds new findings that are serious and on newly changed lines
    # (P0 anywhere). From the 4th review on, only P0/P1 can be new.
    suppressed = 0
    if followup:
        new_ok = ("P0", "P1") if rounds >= 3 else ("P0", "P1", "P2")
        kept = [f for f in findings if f.get("prior_id") in prior_by_id or f["severity"] == "P0"
                or (f["severity"] in new_ok and f.get("line") in delta.get(f.get("path"), ()))]
        suppressed, findings = len(findings) - len(kept), kept
    findings.sort(key=lambda f: SEVERITIES.index(f["severity"]))
    valid = commentable_lines(diff)
    threads = open_bot_threads(a.repo, a.pr)
    open_titles = {t["title"] for t in threads}
    anchored, unanchored, repeated = [], [], 0
    for f in findings:
        line = f.get("line")
        if f.get("title") in open_titles:  # already has an open bot thread; do not repeat it
            repeated += 1
        elif isinstance(line, int) and line in valid.get(f.get("path"), {}) and len(anchored) < MAX_INLINE:
            anchored.append(f)
        else:
            unanchored.append(f)

    review = {
        "commit_id": head,
        "event": "COMMENT",
        "body": render(result, findings, unanchored, prior_status, a.errors, a.cut_short, suppressed, a.mode,
                       followup, a.model, elapsed, head, calls, skipped, sampled, broad, alerts),
        "comments": [{"path": f["path"], "line": f["line"], "side": "RIGHT",
                      "body": inline_body(f, valid[f["path"]][f["line"]])} for f in anchored],
    }
    closed = close_fixed_threads(a.repo, a.pr, prior_status, threads, head, a.dry_run)
    # The bot approves only when nothing is left: no finding and no unresolved thread of its own.
    approve = not findings and not threads and not a.errors and not a.cut_short
    if approve:
        review["event"] = "APPROVE"
    print(json.dumps({"elapsed_s": round(elapsed), "followup": followup, "round": rounds + 1,
                      "findings": len(findings),
                      "inline": len(anchored), "summary_only": len(unanchored), "already_posted": repeated,
                      "suppressed": suppressed, "threads_resolved": closed, "errors": a.errors,
                      "cut_short": a.cut_short}))
    if a.dry_run:
        print(json.dumps(review, ensure_ascii=False, indent=2))
        return
    posted = gh("api", "-X", "POST", f"repos/{a.repo}/pulls/{a.pr}/reviews", "--input", "-",
                input_text=json.dumps(review), check=not approve)
    if posted.returncode != 0:
        log(f"could not approve ({posted.stderr.strip()[:200]}); posting a comment instead")
        review["event"] = "COMMENT"
        gh("api", "-X", "POST", f"repos/{a.repo}/pulls/{a.pr}/reviews", "--input", "-", input_text=json.dumps(review))
    print(f"review posted ({review['event'].lower()})")


def full_review(a, batches, ctx_path, pr_text):
    """One agent reviews the PR from the three perspectives and verifies its own findings."""
    persp = write_temp(perspectives_prompt([p for b in batches for p, _ in b]))
    try:
        prompts = [os.path.join(HERE, "prompts", "review.md"), persp] + ([ctx_path] if ctx_path else [])
        return tracked(1)("review", lambda: run_pi(
            a, prompts, pr_text + "\nReview this PR. Return only the JSON object.", a.until, "review"))
    finally:
        os.unlink(persp)


def followup_review(a, batches, ctx_path, batch_text, prior):
    """One follow-up call per batch of the delta; each earlier finding is re-checked in the batch that touches
    its file, or in the first batch when no batch does."""
    paths = [{p for p, _ in b} for b in batches] or [set()]
    touched = set().union(*paths)
    track = tracked(len(paths))

    def call(i):
        mine = [f for f in prior["findings"] if f.get("path") in paths[i] or (i == 0 and f.get("path") not in touched)]
        where = f"follow-up (batch {i + 1}/{len(paths)})"
        try:
            return track(where, lambda: run_pi(
                a, [os.path.join(HERE, "prompts", "followup.md")] + ([ctx_path] if ctx_path else []),
                batch_text(i, dict(prior, findings=mine)) + "\nDo the follow-up review. Return only the JSON object.",
                a.until, where))
        except Exception as e:  # its earlier findings then count as still open
            log(f"{where} failed: {e}")
            a.errors.append(where)
            return {}
    with ThreadPoolExecutor(max_workers=max(1, a.parallel)) as pool:
        parts = list(pool.map(call, range(len(paths))))
    return {"summary": " ".join((r.get("summary") or "").strip() for r in parts).strip(),
            "prior_findings": [p for r in parts for p in r.get("prior_findings") or []],
            "findings": [f for r in parts for f in r.get("findings") or []]}

if __name__ == "__main__":
    main()
