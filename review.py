#!/usr/bin/env python3
"""Müsli: review a GitHub PR with Pi (GLM Coding Plan) and post the result as one PR review.

Five lens reviewers run in parallel (correctness, contracts & tests, security, performance,
simplicity & scope), each with checklists from the vendored awesome-skills/code-review-skill.
A synthesis pass verifies, de-duplicates and writes the final review. On re-runs, earlier
findings are re-checked; fixed ones get a reply and their thread is resolved.

Usage: review.py --repo owner/name --pr N --workdir <checkout at PR head>
                 [--model zai/glm-5.3] [--thinking high] [--parallel 5] [--pi pi] [--dry-run]
Needs: gh (authenticated via GH_TOKEN), pi, ZAI_API_KEY in the environment.
Project rules come from CLAUDE.md / AGENTS.md / .github/ai-review.md on the BASE branch.
"""
import argparse
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
GUIDES = os.path.join(HERE, "vendor", "code-review-skill", "reference")
BOT_NAME = "Müsli"
MARKER = "muesli:v1"
CONTEXT_FILES = ["CLAUDE.md", "AGENTS.md", ".github/ai-review.md"]
MAX_DIFF_CHARS = 200_000
MAX_INLINE = 25
SEVERITIES = ["P0", "P1", "P2", "P3", "nit"]
SEVERITY_LABEL = {"P0": "🔴 P0", "P1": "🟠 P1", "P2": "🟡 P2", "P3": "🔵 P3", "nit": "⚪ nit"}
PRIOR_STATUS_LABEL = {"resolved": "✅ fixed", "still_open": "⏳ still open",
                      "obsolete": "🗑️ no longer applies", "rejected": "🚫 not a defect"}

LENSES = {
    "correctness": (
        "Correctness: logic errors, edge cases, off-by-one, null/empty handling, error handling, "
        "panics/exceptions, state and lifecycle, concurrency and locking.",
        ["common-bugs-checklist.md", "cross-cutting/error-handling-principles.md",
         "cross-cutting/async-concurrency-patterns.md"]),
    "contracts": (
        "Contracts, integration and tests: API/wire compatibility, data/schema/migration safety, callers "
        "that break, backwards compatibility, config. Tests: which existing tests break (name them), "
        "what the change leaves untested.",
        ["code-review-best-practices.md"]),
    "security": (
        "Security: authentication and authorization, input validation, injection (SQL, command, XSS), "
        "secrets, unsafe defaults, abuse paths (rate limits, enumeration), data exposure.",
        ["security-review-guide.md", "cross-cutting/sql-injection-prevention.md",
         "cross-cutting/xss-prevention.md"]),
    "performance": (
        "Performance and resources: algorithmic complexity, N+1 queries, unbounded growth, blocking calls "
        "in async code, leaks, needless work.",
        ["performance-review-guide.md", "cross-cutting/n-plus-one-queries.md"]),
    "simplicity": (
        "Simplicity and scope (KISS). Be strict; this team values simple, minimal changes that follow the "
        "project's existing structure. Use the PR description and the project context (CLAUDE.md) as the "
        "reference for intended scope. Flag: files or changes outside the stated purpose; unnecessary "
        "churn (renames, reformatting, rewrites nobody asked for); over-engineering (new abstractions, "
        "layers, options, generics or config for a single use); more code than the problem needs; "
        "duplicating an existing helper instead of reusing it; not following existing patterns, naming "
        "or folder layout; new dependencies that are not clearly necessary; dead code. For each, say "
        "what the simpler alternative is. Also check whether the description matches what the code does.",
        ["code-quality-universal.md"]),
}
LANGUAGE_GUIDES = {".rs": ["rust.md"], ".ts": ["typescript.md"], ".tsx": ["typescript.md", "react.md"],
                   ".jsx": ["react.md"], ".py": ["python.md"], ".kt": ["kotlin.md"], ".php": ["php.md"],
                   ".vue": ["vue.md"], ".go": ["go.md"], ".java": ["java.md"], ".swift": ["swift.md"],
                   ".dart": ["dart.md"], ".css": ["css-less-sass.md"], ".scss": ["css-less-sass.md"]}


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


def lens_prompt(name, files):
    focus, guides = LENSES[name]
    if name == "correctness":
        exts = {os.path.splitext(f["path"])[1] for f in files}
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
    """Latest Müsli review's findings (from its hidden marker), or None."""
    for r in reversed(reviews):
        m = re.search(rf"<!-- {MARKER} head=(\w+) findings=([A-Za-z0-9+/=]*) -->", r.get("body") or "")
        if m:
            try:
                findings = json.loads(base64.b64decode(m.group(2)) or b"[]")
            except ValueError:
                findings = []
            return {"head": m.group(1), "findings": findings}
    return None


def pr_prompt(meta, diff, inline_comments, prior):
    convo = [f"- {c['author']['login']}: {c['body']}" for c in meta.get("comments", [])]
    convo += [f"- {r['author']['login']} (review, {r['state']}): {r['body']}"
              for r in meta.get("reviews", []) if r.get("body") and MARKER not in r["body"]]
    convo += [f"- {c['user']['login']} on {c['path']}:{c.get('line')}: {c['body']}" for c in inline_comments]
    files = "\n".join(f"- {f['path']} (+{f['additions']}/-{f['deletions']})" for f in meta["files"])
    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS] + "\n[diff truncated; use the read tool for the remaining files]"
    prior_txt = "(none: this is the first Müsli review of this PR)"
    if prior and prior["findings"]:
        prior_txt = (f"Previous Müsli review was at commit {prior['head'][:7]}. Its findings:\n" +
                     "\n".join(f"- [{f.get('severity')}] {f.get('title')} ({f.get('path')}:{f.get('line')})"
                               for f in prior["findings"]))
    return (
        f"PR title: {meta['title']}\n"
        f"Author: {meta['author']['login']}  |  {meta['headRefName']} -> {meta['baseRefName']}\n\n"
        f"PR description:\n<<<\n{meta['body'] or '(empty)'}\n>>>\n\n"
        f"Changed files:\n{files}\n\n"
        f"Discussion so far (untrusted):\n{chr(10).join(convo) or '(none)'}\n\n"
        f"Prior Müsli findings:\n{prior_txt}\n\n"
        f"Diff:\n<<<\n{diff}\n>>>\n"
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
    return extract_json(run.stdout)


def write_temp(text):
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as fh:
        fh.write(text)
    return fh.name


def run_lens(a, name, files, ctx_path, pr_text):
    lens_path = write_temp(lens_prompt(name, files))
    try:
        out = run_pi(a, [os.path.join(HERE, "prompts", "base.md"), lens_path] + ([ctx_path] if ctx_path else []),
                     pr_text + f"\nReview this PR through the `{name}` lens only. Return only the JSON object.",
                     timeout=900)
        return name, out, None
    except Exception as e:  # one failed lens must not sink the review
        return name, None, str(e)
    finally:
        os.unlink(lens_path)


def verdict_of(findings):
    """Host-owned verdict: only P0/P1 block; the model's own opinion is advisory."""
    if any(f.get("severity") in ("P0", "P1") for f in findings):
        return "⛔ Changes requested"
    if any(f.get("severity") in ("P2", "P3") for f in findings):
        return "💬 Worth a look"
    return "✅ Looks good"


def finding_md(f, with_location):
    loc = f"`{f.get('path') or 'repo-wide'}{':' + str(f['line']) if f.get('line') else ''}` " if with_location else ""
    conf = f" · confidence {f['confidence']:.1f}" if isinstance(f.get("confidence"), (int, float)) else ""
    lenses = f" · {', '.join(f['lenses'])}" if isinstance(f.get("lenses"), list) and f["lenses"] else ""
    return f"{SEVERITY_LABEL.get(f.get('severity'), f.get('severity'))} {loc}**{f.get('title', '')}**{conf}{lenses}"


def inline_body(f, is_added_line):
    body = f"{finding_md(f, False)}\n\n{f.get('body', '').strip()}"
    suggestion = f.get("suggestion")
    if isinstance(suggestion, str) and suggestion.strip() and is_added_line:
        body += f"\n\n```suggestion\n{suggestion.rstrip()}\n```"
    return body


def render(result, findings, unanchored, lens_errors, model, elapsed, head_sha):
    dc = result.get("description_consistency") or {}
    parts = [f"## 🐔 {BOT_NAME}'s thoughts: {verdict_of(findings)}", (result.get("summary") or "").strip()]
    parts.append(f"**Description vs. changes:** {'✅' if dc.get('ok') else '⚠️'} {(dc.get('notes') or '').strip()}")
    if (result.get("scope_notes") or "").strip():
        parts.append(f"**Simplicity & scope:** {result['scope_notes'].strip()}")
    if findings:
        counts = ", ".join(f"{sum(f.get('severity') == s for f in findings)}× {s}" for s in SEVERITIES
                           if any(f.get("severity") == s for f in findings))
        parts.append(f"**Findings:** {counts}")
    prior = result.get("prior_findings") or []
    if prior:
        parts.append("**Earlier findings:**\n" + "\n".join(
            f"- {PRIOR_STATUS_LABEL.get(p.get('status'), p.get('status'))}: {p.get('title')}"
            f"{' — ' + p['note'] if p.get('note') else ''}" for p in prior))
    if unanchored:
        parts.append("**Other findings** (not tied to a changed line):\n" + "\n".join(
            f"- {finding_md(f, True)}: {f.get('body', '').strip()}" for f in unanchored))
    if (result.get("testing_notes") or "").strip():
        parts.append(f"**Testing:** {result['testing_notes'].strip()}")
    if lens_errors:
        parts.append("⚠️ Incomplete: these lenses failed and were skipped: " + ", ".join(lens_errors))
    carried = [{k: f.get(k) for k in ("severity", "title", "path", "line")} for f in findings]
    encoded = base64.b64encode(json.dumps(carried, ensure_ascii=False).encode()).decode()
    lenses_ok = len(LENSES) - len(lens_errors)
    parts.append(f"<sub>{model} · Pi · {lenses_ok}/{len(LENSES)} lenses + synthesis · {elapsed:.0f}s · {head_sha[:7]}</sub>\n"
                 f"<!-- {MARKER} head={head_sha} findings={encoded} -->")
    return "\n\n".join(p for p in parts if p)


THREADS_QUERY = """query($o:String!,$n:String!,$p:Int!){repository(owner:$o,name:$n){pullRequest(number:$p){
reviewThreads(first:100){nodes{id isResolved comments(first:1){nodes{databaseId body author{login}}}}}}}}"""
RESOLVE_MUTATION = "mutation($t:ID!){resolveReviewThread(input:{threadId:$t}){thread{isResolved}}}"
REPLY = {"resolved": "✅ Fixed in {sha}.", "obsolete": "🗑️ No longer applies as of {sha}: the code was removed or rewritten.",
         "rejected": "🚫 On re-check at {sha} this is not a defect."}


def close_fixed_threads(repo, pr, prior_status, head, dry_run):
    """Reply to and resolve Müsli's own inline threads whose finding is no longer open."""
    done = [p for p in prior_status if p.get("status") in REPLY]
    if not done:
        return 0
    owner, name = repo.split("/")
    data = json.loads(gh("api", "graphql", "-f", f"query={THREADS_QUERY}", "-F", f"o={owner}",
                         "-F", f"n={name}", "-F", f"p={pr}").stdout)
    threads = data["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
    closed = 0
    for p in done:
        for t in threads:
            first = (t["comments"]["nodes"] or [None])[0]
            if (t["isResolved"] or not first or not (first["author"] or {}).get("login", "").startswith("github-actions")
                    or f"**{p.get('title')}**" not in first["body"]):
                continue
            text = REPLY[p["status"]].format(sha=head[:7]) + (f" {p['note']}" if p.get("note") else "")
            if dry_run:
                print(f"would resolve thread {t['id']}: {text}")
            else:
                gh("api", "-X", "POST", f"repos/{repo}/pulls/{pr}/comments/{first['databaseId']}/replies",
                   "-f", f"body={text}")
                res = gh("api", "graphql", "-f", f"query={RESOLVE_MUTATION}", "-F", f"t={t['id']}", check=False)
                if res.returncode != 0:
                    print(f"warning: could not resolve thread {t['id']}: {res.stderr.strip()}", file=sys.stderr)
            t["isResolved"] = True
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
    ap.add_argument("--pi", default="pi")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    meta = json.loads(gh("pr", "view", str(a.pr), "-R", a.repo, "--json",
                         "title,body,author,baseRefName,headRefName,headRefOid,files,comments,reviews").stdout)
    inline = json.loads(gh("api", f"repos/{a.repo}/pulls/{a.pr}/comments", "--paginate").stdout)
    raw_reviews = json.loads(gh("api", f"repos/{a.repo}/pulls/{a.pr}/reviews", "--paginate").stdout)
    diff = gh("pr", "diff", str(a.pr), "-R", a.repo).stdout
    prior = previous_review(raw_reviews)
    pr_text = pr_prompt(meta, diff, inline, prior)
    context = base_context(a.repo, meta["baseRefName"])
    ctx_path = write_temp(context) if context else None

    started = time.time()
    try:
        with ThreadPoolExecutor(max_workers=max(1, a.parallel)) as pool:
            results = list(pool.map(lambda n: run_lens(a, n, meta["files"], ctx_path, pr_text), LENSES))
        candidates, notes, lens_errors = [], [], []
        for name, out, err in results:
            if err:
                lens_errors.append(name)
                print(f"lens {name} failed: {err}", file=sys.stderr)
                continue
            notes.append(f"- {name}: {out.get('notes', '')}")
            candidates += [dict(f, lens=name) for f in out.get("findings", [])]
        if len(lens_errors) == len(LENSES):
            sys.exit("all lenses failed")
        synthesis_msg = (pr_text + "\nLens notes:\n" + "\n".join(notes) +
                         "\n\nCandidate findings from the lens reviewers (JSON):\n" +
                         json.dumps(candidates, ensure_ascii=False, indent=1) +
                         "\n\nProduce the final review. Return only the JSON object.")
        result = run_pi(a, [os.path.join(HERE, "prompts", "synthesis.md")] + ([ctx_path] if ctx_path else []),
                        synthesis_msg, timeout=900)
    finally:
        if ctx_path:
            os.unlink(ctx_path)
    elapsed = time.time() - started

    findings = [f for f in result.get("findings", []) if f.get("severity") in SEVERITIES]
    findings.sort(key=lambda f: SEVERITIES.index(f["severity"]))
    valid = commentable_lines(diff)
    # Do not repeat an inline comment Müsli already left on the same line with the same title.
    posted = {(c["path"], c.get("line"), t) for c in inline if c["user"]["login"].endswith("[bot]")
              for t in re.findall(r"\*\*(.+?)\*\*", c["body"])[:1]}
    anchored, unanchored, skipped = [], [], 0
    for f in findings:
        line = f.get("line")
        if isinstance(line, int) and line in valid.get(f.get("path"), {}) and len(anchored) < MAX_INLINE:
            if (f["path"], line, f.get("title")) in posted:
                skipped += 1
                continue
            anchored.append(f)
        else:
            unanchored.append(f)

    head = meta["headRefOid"]
    review = {
        "commit_id": head,
        "event": "COMMENT",
        "body": render(result, findings, unanchored, lens_errors, a.model, elapsed, head),
        "comments": [{"path": f["path"], "line": f["line"], "side": "RIGHT",
                      "body": inline_body(f, valid[f["path"]][f["line"]])} for f in anchored],
    }
    closed = close_fixed_threads(a.repo, a.pr, result.get("prior_findings") or [], head, a.dry_run)
    print(json.dumps({"elapsed_s": round(elapsed), "candidates": len(candidates), "findings": len(findings),
                      "inline": len(anchored), "summary_only": len(unanchored), "already_posted": skipped,
                      "threads_resolved": closed, "lens_errors": lens_errors}))
    if a.dry_run:
        print(json.dumps(review, ensure_ascii=False, indent=2))
        return
    gh("api", "-X", "POST", f"repos/{a.repo}/pulls/{a.pr}/reviews", "--input", "-",
       input_text=json.dumps(review))
    print("review posted")


if __name__ == "__main__":
    main()
