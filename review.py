#!/usr/bin/env python3
"""Müsli: review a GitHub PR with Pi (GLM Coding Plan) and post the result as one PR review.

Usage: review.py --repo owner/name --pr N --workdir <checkout at PR head> --system system.md
                 [--model zai/glm-5.3] [--thinking high] [--pi pi] [--dry-run]
Needs: gh (authenticated via GH_TOKEN), pi, ZAI_API_KEY in the environment.

Project rules come from CLAUDE.md / AGENTS.md / .github/ai-review.md on the BASE branch,
so a PR cannot rewrite its own review rules. Findings of the previous Müsli review are
carried in a hidden marker and re-checked on every run (resolved / still open / ...).
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

BOT_NAME = "Müsli"
MARKER = "muesli:v1"
CONTEXT_FILES = ["CLAUDE.md", "AGENTS.md", ".github/ai-review.md"]
MAX_DIFF_CHARS = 200_000
MAX_INLINE = 25
SEVERITIES = ["P0", "P1", "P2", "P3", "nit"]
SEVERITY_LABEL = {"P0": "🔴 P0", "P1": "🟠 P1", "P2": "🟡 P2", "P3": "🔵 P3", "nit": "⚪ nit"}
PRIOR_STATUS_LABEL = {"resolved": "✅ resolved", "still_open": "⏳ still open",
                      "obsolete": "🗑️ obsolete", "rejected": "🚫 not a defect"}


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


def build_prompt(meta, diff, inline_comments, prior):
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
        f"Diff:\n<<<\n{diff}\n>>>\n\n"
        "Review the PR and return only the JSON object described in your instructions."
    )


def extract_json(text):
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("model output has no JSON object")
    return json.loads(text[start : end + 1])


def verdict_of(findings):
    """Host-owned verdict: only P0/P1 block; the model's own verdict is advisory."""
    if any(f.get("severity") in ("P0", "P1") for f in findings):
        return "⛔ Changes requested"
    if any(f.get("severity") in ("P2", "P3") for f in findings):
        return "💬 Worth a look"
    return "✅ Looks good"


def finding_md(f, with_location):
    loc = f"`{f.get('path') or 'repo-wide'}{':' + str(f['line']) if f.get('line') else ''}` " if with_location else ""
    conf = f" · confidence {f['confidence']:.1f}" if isinstance(f.get("confidence"), (int, float)) else ""
    return f"{SEVERITY_LABEL.get(f.get('severity'), f.get('severity'))} {loc}**{f.get('title', '')}**{conf}"


def inline_body(f, is_added_line):
    body = f"{finding_md(f, False)}\n\n{f.get('body', '').strip()}"
    suggestion = f.get("suggestion")
    if isinstance(suggestion, str) and suggestion.strip() and is_added_line and not f.get("end_line"):
        body += f"\n\n```suggestion\n{suggestion.rstrip()}\n```"
    return body


def render(result, findings, unanchored, prior_status, model, elapsed, head_sha):
    dc = result.get("description_consistency") or {}
    parts = [f"## 🥣 {BOT_NAME}'s thoughts: {verdict_of(findings)}", (result.get("summary") or "").strip()]
    parts.append(f"**Description vs. changes:** {'✅' if dc.get('ok') else '⚠️'} {(dc.get('notes') or '').strip()}")
    if (result.get("scope_notes") or "").strip():
        parts.append(f"**Out-of-scope / unnecessary changes:** {result['scope_notes'].strip()}")
    if findings:
        counts = ", ".join(f"{sum(f.get('severity') == s for f in findings)}× {s}" for s in SEVERITIES
                           if any(f.get("severity") == s for f in findings))
        parts.append(f"**Findings:** {counts}")
    if prior_status:
        parts.append("**Prior findings:**\n" + "\n".join(
            f"- {PRIOR_STATUS_LABEL.get(p.get('status'), p.get('status'))}: {p.get('title')}"
            f"{' — ' + p['note'] if p.get('note') else ''}" for p in prior_status))
    if unanchored:
        parts.append("**Other findings** (not tied to a changed line):\n" + "\n".join(
            f"- {finding_md(f, True)}: {f.get('body', '').strip()}" for f in unanchored))
    if (result.get("testing_notes") or "").strip():
        parts.append(f"**Testing:** {result['testing_notes'].strip()}")
    carried = [{k: f.get(k) for k in ("severity", "title", "path", "line")} for f in findings]
    encoded = base64.b64encode(json.dumps(carried, ensure_ascii=False).encode()).decode()
    parts.append(f"<sub>{model} · Pi · {elapsed:.0f}s · {head_sha[:7]}</sub>\n"
                 f"<!-- {MARKER} head={head_sha} findings={encoded} -->")
    return "\n\n".join(p for p in parts if p)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--pr", required=True, type=int)
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--system", required=True)
    ap.add_argument("--model", default="zai/glm-5.3")
    ap.add_argument("--thinking", default="high")
    ap.add_argument("--pi", default="pi")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    meta = json.loads(gh("pr", "view", str(a.pr), "-R", a.repo, "--json",
                         "title,body,author,baseRefName,headRefName,headRefOid,files,comments,reviews").stdout)
    inline = json.loads(gh("api", f"repos/{a.repo}/pulls/{a.pr}/comments", "--paginate").stdout)
    raw_reviews = json.loads(gh("api", f"repos/{a.repo}/pulls/{a.pr}/reviews", "--paginate").stdout)
    diff = gh("pr", "diff", str(a.pr), "-R", a.repo).stdout
    prior = previous_review(raw_reviews)

    cmd = [a.pi, "-p", "--no-session", "--offline", "--model", a.model, "--thinking", a.thinking,
           "--tools", "read,grep,find,ls", "--no-extensions", "--no-skills",
           "--no-prompt-templates", "--no-context-files", "--no-mcp",
           "--append-system-prompt", a.system]
    context = base_context(a.repo, meta["baseRefName"])
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as ctx_file:
        ctx_file.write(context)
    if context:
        cmd += ["--append-system-prompt", ctx_file.name]
    started = time.time()
    try:
        run = subprocess.run(cmd, input=build_prompt(meta, diff, inline, prior), cwd=a.workdir,
                             capture_output=True, text=True, timeout=1200)
    finally:
        os.unlink(ctx_file.name)
    elapsed = time.time() - started
    if run.returncode != 0:
        sys.exit(f"pi failed ({run.returncode}): {run.stderr.strip()[-2000:]}")
    try:
        result = extract_json(run.stdout)
    except ValueError as e:
        sys.exit(f"{e}\n--- pi output ---\n{run.stdout[-4000:]}")

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
        "body": render(result, findings, unanchored, result.get("prior_findings") or [],
                       a.model, elapsed, head),
        "comments": [{"path": f["path"], "line": f["line"], "side": "RIGHT",
                      "body": inline_body(f, valid[f["path"]][f["line"]])} for f in anchored],
    }
    print(json.dumps({"elapsed_s": round(elapsed), "findings": len(findings), "inline": len(anchored),
                      "summary_only": len(unanchored), "already_posted": skipped,
                      "prior": len(result.get("prior_findings") or [])}))
    if a.dry_run:
        print(json.dumps(review, ensure_ascii=False, indent=2))
        return
    gh("api", "-X", "POST", f"repos/{a.repo}/pulls/{a.pr}/reviews", "--input", "-",
       input_text=json.dumps(review))
    print("review posted")


if __name__ == "__main__":
    main()
