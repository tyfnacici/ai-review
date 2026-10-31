#!/usr/bin/env python3
"""Review a GitHub PR with Pi (GLM Coding Plan) and post the result as a PR review.

Usage: review.py --repo owner/name --pr N --workdir <checkout at PR head>
                 --system system.md [--dry-run]
Project context comes from CLAUDE.md / AGENTS.md / .github/ai-review.md on the base branch.
Needs: gh (authenticated), pi, ZAI_API_KEY in the environment.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time

SEVERITY_ICON = {"P0": "🔴 P0", "P1": "🟠 P1", "P2": "🟡 P2", "P3": "🔵 P3", "nit": "⚪ nit"}
MAX_DIFF_CHARS = 200_000
# Read from the base branch in this order; .github/ai-review.md is an optional extra.
CONTEXT_FILES = ["CLAUDE.md", "AGENTS.md", ".github/ai-review.md"]


def gh(*args, input_text=None):
    out = subprocess.run(["gh", *args], input=input_text, capture_output=True, text=True)
    if out.returncode != 0:
        sys.exit(f"gh {' '.join(args[:3])} failed: {out.stderr.strip()}")
    return out.stdout


def base_context(repo, base_ref):
    """Project docs from the BASE branch, so a PR cannot rewrite its own review rules."""
    parts = []
    for path in CONTEXT_FILES:
        out = subprocess.run(["gh", "api", f"repos/{repo}/contents/{path}?ref={base_ref}",
                              "-H", "Accept: application/vnd.github.raw"],
                             capture_output=True, text=True)
        if out.returncode == 0 and out.stdout.strip():
            parts.append(f"# Proje bağlamı: {path} ({base_ref} dalından)\n\n{out.stdout.strip()}")
    return "\n\n".join(parts)


def commentable_lines(diff):
    """Map path -> set of new-file line numbers that GitHub accepts for RIGHT-side comments."""
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
            lines.setdefault(path, set()).add(new_line)
            new_line += 1
    return lines


def extract_json(text):
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("model output has no JSON object")
    return json.loads(text[start : end + 1])


def build_prompt(meta, diff, inline_comments):
    convo = [f"- {c['author']['login']}: {c['body']}" for c in meta.get("comments", [])]
    convo += [f"- {r['author']['login']} (review, {r['state']}): {r['body']}" for r in meta.get("reviews", []) if r.get("body")]
    convo += [f"- {c['user']['login']} @ {c['path']}:{c.get('line')}: {c['body']}" for c in inline_comments]
    files = "\n".join(f"- {f['path']} (+{f['additions']}/-{f['deletions']})" for f in meta["files"])
    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS] + "\n[diff kesildi; geri kalan dosyalar için read aracını kullan]"
    return (
        f"PR başlığı: {meta['title']}\n"
        f"Yazar: {meta['author']['login']}  |  {meta['headRefName']} -> {meta['baseRefName']}\n\n"
        f"PR açıklaması:\n<<<\n{meta['body'] or '(boş)'}\n>>>\n\n"
        f"Değişen dosyalar:\n{files}\n\n"
        f"Önceki yorumlar:\n{chr(10).join(convo) or '(yok)'}\n\n"
        f"Diff:\n<<<\n{diff}\n>>>\n\n"
        "İncele ve sadece istenen JSON'u döndür."
    )


def render(result, unanchored, model, elapsed, head_sha):
    verdict = {"approve": "✅ Onaylanabilir", "request_changes": "⛔ Değişiklik gerekli"}.get(result.get("verdict"), "💬 Yorum")
    dc = result.get("description_consistency") or {}
    parts = [
        f"## 🤖 AI Review: {verdict}",
        result.get("summary", "").strip(),
        f"**PR açıklaması ile tutarlılık:** {'✅' if dc.get('ok') else '⚠️'} {dc.get('notes', '').strip()}",
    ]
    if result.get("scope_notes", "").strip():
        parts.append(f"**Kapsam dışı / gereksiz değişiklikler:** {result['scope_notes'].strip()}")
    if unanchored:
        parts.append("**Diğer bulgular:**\n" + "\n".join(
            f"- {SEVERITY_ICON.get(f.get('severity'), f.get('severity'))} `{f.get('path') or 'genel'}"
            f"{':' + str(f['line']) if f.get('line') else ''}` **{f.get('title', '')}**: {f.get('body', '')}"
            for f in unanchored))
    parts.append(f"<sub>{model} · Pi · {elapsed:.0f} sn · {head_sha[:7]}</sub>\n<!-- ai-review:{head_sha} -->")
    return "\n\n".join(p for p in parts if p)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--pr", required=True, type=int)
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--system", required=True)
    ap.add_argument("--model", default="zai/glm-5.3")
    ap.add_argument("--pi", default="pi")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    meta = json.loads(gh("pr", "view", str(a.pr), "-R", a.repo, "--json",
                         "title,body,author,baseRefName,headRefName,headRefOid,files,comments,reviews"))
    inline = json.loads(gh("api", f"repos/{a.repo}/pulls/{a.pr}/comments", "--paginate"))
    diff = gh("pr", "diff", str(a.pr), "-R", a.repo)

    cmd = [a.pi, "-p", "--no-session", "--offline", "--model", a.model,
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
        run = subprocess.run(cmd, input=build_prompt(meta, diff, inline), cwd=a.workdir,
                             capture_output=True, text=True, timeout=900)
    finally:
        os.unlink(ctx_file.name)
    elapsed = time.time() - started
    if run.returncode != 0:
        sys.exit(f"pi failed ({run.returncode}): {run.stderr.strip()[-2000:]}")
    try:
        result = extract_json(run.stdout)
    except ValueError as e:
        sys.exit(f"{e}\n--- pi output ---\n{run.stdout[-4000:]}")

    valid = commentable_lines(diff)
    anchored, unanchored = [], []
    for f in result.get("findings", []):
        line = f.get("line")
        if isinstance(line, int) and line in valid.get(f.get("path"), ()):
            anchored.append(f)
        else:
            unanchored.append(f)

    head = meta["headRefOid"]
    review = {
        "commit_id": head,
        "event": "COMMENT",
        "body": render(result, unanchored, a.model, elapsed, head),
        "comments": [{"path": f["path"], "line": f["line"], "side": "RIGHT",
                      "body": f"{SEVERITY_ICON.get(f.get('severity'), f.get('severity'))} **{f.get('title', '')}**\n\n{f.get('body', '')}"}
                     for f in anchored],
    }
    print(json.dumps({"elapsed_s": round(elapsed), "anchored": len(anchored),
                      "unanchored": len(unanchored), "verdict": result.get("verdict")}))
    if a.dry_run:
        print(json.dumps(review, ensure_ascii=False, indent=2))
        return
    gh("api", "-X", "POST", f"repos/{a.repo}/pulls/{a.pr}/reviews", "--input", "-",
       input_text=json.dumps(review))
    print("review posted")


if __name__ == "__main__":
    main()
