#!/usr/bin/env python3
"""AI PR review: review a GitHub PR with the Pi coding agent and post the result as one PR review.

Three lens reviewers run in parallel (correctness & contracts, security & performance,
simplicity & scope), each with checklists from the vendored awesome-skills/code-review-skill.
A synthesis pass verifies, de-duplicates and writes the final review. Every later push gets one
short follow-up call instead: earlier findings are re-checked (fixed ones get a reply and their
thread is resolved) and only serious problems in the new changes are added, so a PR converges.
The diff comes from the local checkout, so PRs of any size work; a large diff is split into batches
and every lens (or follow-up call) reviews every batch.

Usage: review.py --repo owner/name --pr N --workdir <checkout at PR head>
                 [--mode feature|release] [--model zai/glm-5.3] [--thinking high] [--parallel 3]
                 [--batch-chars 120000] [--pi pi] [--dry-run]
Feature mode reviews one change in depth; release mode (e.g. staging -> main) looks at the combined effect.
Needs: gh (authenticated via GH_TOKEN), pi, and the model provider's API key in the environment (e.g. ZAI_API_KEY).
Project rules come from CLAUDE.md / AGENTS.md / .github/ai-review.md on the BASE branch.
"""
import argparse
import base64
import itertools
import json
import os
import re
import subprocess
import sys
import tempfile
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
BULK_MIN, BULK_SAMPLE = 10, 2  # a folder with >= 10 changed data files of one type: 2 go in the diff, the rest are listed
MAX_INLINE = 25
LENS_TIMEOUT = 1200  # seconds per Pi call
SEVERITIES = ["P0", "P1", "P2", "P3", "nit"]
PRIOR_STATUS_LABEL = {"resolved": "fixed", "still_open": "still open", "deferred": "deferred",
                      "obsolete": "no longer applies", "rejected": "not a defect"}
MIN_MINOR_CONFIDENCE = 0.7  # P3/nit below this are dropped

LENSES = {
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
    "Do not re-review individual lines in depth. Look at the combined effect through your lens: interactions "
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
    """Wraps a call so each start and finish is logged with how many are done and an estimate of the time left."""
    done, t0 = itertools.count(1), time.time()

    def run(where, fn):
        log(f"{where}: started")
        start, ok = time.time(), False
        try:
            out = fn()
            ok = not (isinstance(out, tuple) and out[2])  # run_lens reports its error instead of raising
            return out
        finally:
            k = next(done)
            left = (time.time() - t0) / k * (total - k)
            log(f"{where}: {'done' if ok else 'failed'} in {time.time() - start:.0f}s ({k}/{total}"
                + (f", about {left / 60:.0f} min left)" if k < total else ")"))
    return run


def gh(*args, input_text=None, check=True):
    out = subprocess.run(["gh", *args], input=input_text, capture_output=True, text=True)
    if check and out.returncode != 0:
        sys.exit(f"gh {' '.join(args[:3])} failed: {out.stderr.strip()}")
    return out


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


def plan_batches(file_diffs, limit):
    """Pack file diffs into batches of about `limit` characters. Returns (batches, skipped, sampled): lockfiles,
    binaries and minified files are skipped; in a folder with many changed data files only a sample goes in."""
    kind = lambda p: (os.path.dirname(p), os.path.splitext(p)[1].lower())
    bulk = Counter(kind(p) for p, _ in file_diffs if kind(p)[1] in DATA_EXTS)
    seen, batches, size, skipped, sampled = Counter(), [[]], 0, [], []
    for path, text in file_diffs:
        if (os.path.basename(path) in LOCKFILES or path.endswith((".min.js", ".min.css", ".map"))
                or re.search(r"(?m)^Binary files ", text)):
            skipped.append(path)
            continue
        if bulk[kind(path)] >= BULK_MIN:
            seen[kind(path)] += 1
            if seen[kind(path)] > BULK_SAMPLE:
                sampled.append(path)
                continue
        if kind(path)[1] in DATA_EXTS and len(text) > DATA_CHARS:
            text = text[:DATA_CHARS] + f"\n[diff of {path} cut at {DATA_CHARS} characters; read the file for the rest]\n"
        for part in split_large(text, limit):
            if batches[-1] and size + len(part) > limit:
                batches.append([])
                size = 0
            batches[-1].append((path, part))
            size += len(part)
    return [b for b in batches if b], skipped, sampled


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


def lens_prompt(name, paths):
    focus, guides = LENSES[name]
    if name == "correctness":
        exts = {os.path.splitext(p)[1] for p in paths}
        guides = guides + sorted({g for e in exts for g in LANGUAGE_GUIDES.get(e, [])})
    text = [f"# Your lens: {name}\n\n{focus}\n\n# Checklists for this lens\n"]
    for g in guides:
        with open(os.path.join(GUIDES, g), encoding="utf-8") as fh:
            text.append(f"## {g}\n\n{fh.read()}")
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
        notes.append("Data files of the same kind as ones shown in the diff, left out to save space (read any "
                     "that matter): " + ", ".join(sampled))
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


def run_pi(a, prompt_files, message, timeout):
    """One Pi call with read-only tools; returns parsed JSON."""
    cmd = [a.pi, "-p", "--no-session", "--offline", "--model", a.model, "--thinking", a.thinking,
           "--tools", "read,grep,find,ls", "--no-extensions", "--no-skills",
           "--no-prompt-templates", "--no-context-files", "--no-mcp"]
    for f in prompt_files:
        cmd += ["--append-system-prompt", f]
    run = subprocess.run(cmd, input=message, cwd=a.workdir, capture_output=True, text=True, timeout=timeout)
    if run.returncode != 0:
        raise RuntimeError(f"pi exited {run.returncode}: {run.stderr.strip()[-500:]}")
    try:
        return extract_json(run.stdout)
    except ValueError:  # usually one unescaped quote; a cheap no-tools pass repairs it
        fix = subprocess.run([a.pi, "-p", "--no-session", "--offline", "--model", a.model, "--thinking", "off",
                              "--no-tools", "--no-extensions", "--no-skills", "--no-prompt-templates",
                              "--no-context-files", "--no-mcp"],
                             input="Repair this into valid JSON with the same content. Return only the JSON.\n\n"
                                   + run.stdout, capture_output=True, text=True, timeout=300)
        return extract_json(fix.stdout)


def write_temp(text):
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as fh:
        fh.write(text)
    return fh.name


def run_lens(a, name, paths, ctx_path, pr_text):
    lens_path = write_temp(lens_prompt(name, paths))
    try:
        out = run_pi(a, [os.path.join(HERE, "prompts", "base.md"), lens_path] + ([ctx_path] if ctx_path else []),
                     pr_text + f"\nReview this PR through the `{name}` lens only. Return only the JSON object.",
                     timeout=LENS_TIMEOUT)
        return name, out, None
    except Exception as e:  # one failed lens must not sink the review
        return name, None, str(e)
    finally:
        os.unlink(lens_path)


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


def render(result, findings, unanchored, prior_status, lens_errors, suppressed, mode, followup, model, elapsed,
           head_sha, calls, skipped, sampled):
    kinds = [k for k, on in (("release review", mode == "release"), ("follow-up", followup)) if on]
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
    if lens_errors:
        parts.append("**Incomplete:** these review calls failed and their part was skipped: " + ", ".join(lens_errors))
    if skipped or sampled:
        parts.append(f"<sub>Not reviewed line by line: {len(skipped)} lockfile/binary/minified file(s), "
                     f"{len(sampled)} data file(s) of the same kind as reviewed samples.</sub>")
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
    ap.add_argument("--model", default="zai/glm-5.3")
    ap.add_argument("--thinking", default="high")
    ap.add_argument("--parallel", type=int, default=len(LENSES))
    ap.add_argument("--batch-chars", type=int, default=120_000)
    ap.add_argument("--mode", choices=["feature", "release"], default="feature")
    ap.add_argument("--pi", default="pi")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

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
    # First review of a PR: full lens review. Every later push: a short follow-up on the delta.
    follow = delta_since(a.workdir, prior, head)
    followup = follow is not None
    delta, review_diff = follow if followup else (None, diff)
    batches, skipped, sampled = plan_batches(split_files(review_diff), a.batch_chars)
    log(f"{'follow-up' if followup else 'full'} {a.mode} review: {len(batches)} batch(es) of the diff, "
        f"{len(skipped)} skipped and {len(sampled)} sampled file(s), {a.model}, {a.parallel} call(s) at a time")
    mode_note = RELEASE_NOTE if a.mode == "release" else ""
    label = "Changes since the previous review" if followup else "Diff"

    def batch_text(i, prior_part=prior):
        body = "".join(t for _, t in batches[i]) if batches else "(no new commits since the previous review)"
        return mode_note + pr_prompt(meta, files, body, inline, prior_part, label,
                                     batch_note(batches, i, skipped, sampled))

    context = base_context(a.repo, meta["baseRefName"])
    ctx_path = write_temp(context) if context else None

    started = time.time()
    candidates, lens_errors = [], []
    try:
        if followup:
            result = followup_review(a, batches, ctx_path, batch_text, prior)
            calls = f"{max(1, len(batches))} follow-up call(s)"
        else:
            synth_text = batch_text(0) if len(batches) <= 1 else mode_note + pr_prompt(
                meta, files, f"(not repeated here: the lens reviewers saw it in {len(batches)} batches; "
                "read the files to verify findings)", inline, prior, label, batch_note([], 0, skipped, sampled))
            if not context:
                synth_text += (f"\nProject context: none (no {', '.join(CONTEXT_FILES)} on "
                               f"`{meta['baseRefName']}`).\n")
            result, candidates, lens_errors = full_review(a, batches, ctx_path, batch_text, synth_text)
            total = len(LENSES) * max(1, len(batches))
            calls = f"{total - len(lens_errors)}/{total} lens calls + synthesis" + (
                f" ({len(batches)} batches)" if len(batches) > 1 else "")
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
    allowed = SEVERITIES[:3] if a.mode == "release" else SEVERITIES
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
        "body": render(result, findings, unanchored, prior_status, lens_errors, suppressed, a.mode, followup,
                       a.model, elapsed, head, calls, skipped, sampled),
        "comments": [{"path": f["path"], "line": f["line"], "side": "RIGHT",
                      "body": inline_body(f, valid[f["path"]][f["line"]])} for f in anchored],
    }
    closed = close_fixed_threads(a.repo, a.pr, prior_status, threads, head, a.dry_run)
    print(json.dumps({"elapsed_s": round(elapsed), "followup": followup, "round": rounds + 1,
                      "candidates": len(candidates), "findings": len(findings),
                      "inline": len(anchored), "summary_only": len(unanchored), "already_posted": repeated,
                      "suppressed": suppressed, "threads_resolved": closed, "lens_errors": lens_errors}))
    if a.dry_run:
        print(json.dumps(review, ensure_ascii=False, indent=2))
        return
    gh("api", "-X", "POST", f"repos/{a.repo}/pulls/{a.pr}/reviews", "--input", "-",
       input_text=json.dumps(review))
    print("review posted")


def full_review(a, batches, ctx_path, batch_text, synth_text):
    """Every lens on every batch in parallel, then one synthesis pass over all candidates."""
    tasks = [(name, i) for i in range(max(1, len(batches))) for name in LENSES]
    paths = lambda i: [p for p, _ in batches[i]] if batches else []
    label = lambda name, i: f"{name} (batch {i + 1}/{len(batches)})" if len(batches) > 1 else name
    track = tracked(len(tasks))
    with ThreadPoolExecutor(max_workers=max(1, a.parallel)) as pool:
        results = list(pool.map(lambda t: track(label(*t), lambda: run_lens(
            a, t[0], paths(t[1]), ctx_path, batch_text(t[1]))) + (t[1],), tasks))
    candidates, notes, lens_errors = [], [], []
    for name, out, err, i in results:
        where = label(name, i)
        if err:
            lens_errors.append(where)
            print(f"lens {where} failed: {err}", file=sys.stderr)
            continue
        notes.append(f"- {where}: {out.get('notes', '')}")
        candidates += [dict(f, lens=name) for f in out.get("findings", [])]
    if len(lens_errors) == len(tasks):
        sys.exit("all lenses failed")
    candidates = [f for f in candidates if f.get("severity") in SEVERITIES[:3]
                  or (f.get("confidence") or 0) >= MIN_MINOR_CONFIDENCE]
    synthesis_msg = (synth_text + "\nLens notes:\n" + "\n".join(notes) +
                     "\n\nCandidate findings from the lens reviewers (JSON):\n" +
                     json.dumps(candidates, ensure_ascii=False, indent=1) +
                     "\n\nProduce the final review. Return only the JSON object.")
    try:
        result = tracked(1)(f"synthesis of {len(candidates)} candidate(s)", lambda: run_pi(
            a, [os.path.join(HERE, "prompts", "synthesis.md")] + ([ctx_path] if ctx_path else []),
            synthesis_msg, timeout=min(3600, LENS_TIMEOUT + 20 * len(candidates))))
    except Exception as e:  # a big PR's lens work must not be lost to one failed call
        print(f"synthesis failed: {e}", file=sys.stderr)
        lens_errors.append("synthesis")
        result = {"summary": "The synthesis pass failed, so the findings below are the lens reviewers' "
                             "unverified candidates.",
                  "findings": [dict(f, lenses=[f["lens"]]) for f in candidates]}
    return result, candidates, lens_errors


def followup_review(a, batches, ctx_path, batch_text, prior):
    """One follow-up call per batch of the delta; each earlier finding is re-checked in the batch that touches
    its file, or in the first batch when no batch does."""
    paths = [{p for p, _ in b} for b in batches] or [set()]
    touched = set().union(*paths)
    track = tracked(len(paths))

    def call(i):
        mine = [f for f in prior["findings"] if f.get("path") in paths[i] or (i == 0 and f.get("path") not in touched)]
        return track(f"follow-up (batch {i + 1}/{len(paths)})", lambda: run_pi(
            a, [os.path.join(HERE, "prompts", "followup.md")] + ([ctx_path] if ctx_path else []),
            batch_text(i, dict(prior, findings=mine)) + "\nDo the follow-up review. Return only the JSON object.",
            timeout=LENS_TIMEOUT))
    with ThreadPoolExecutor(max_workers=max(1, a.parallel)) as pool:
        parts = list(pool.map(call, range(len(paths))))
    return {"summary": " ".join((r.get("summary") or "").strip() for r in parts).strip(),
            "prior_findings": [p for r in parts for p in r.get("prior_findings") or []],
            "findings": [f for r in parts for f in r.get("findings") or []]}

if __name__ == "__main__":
    main()
