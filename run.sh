#!/bin/sh
# Runner-side entrypoint: loads the model API key from the runner user's home and reviews one PR.
# Called by .github/workflows/review.yml. One review at a time per machine uses the model (model plans often allow
# very few concurrent requests); other runners wait for the lock (at most LOCK_WAIT seconds, then the review is skipped
# with a note) before their time budget starts.
set -eu
: "${GH_TOKEN:?}" "${REPO:?}" "${PR:?}" "${WORKDIR:?}"
HERE=$(cd "$(dirname "$0")" && pwd)
set -a
if [ -f "$HOME/.config/ai-review/env" ]; then . "$HOME/.config/ai-review/env"; fi
set +a
export PI_CODING_AGENT_DIR="${PI_CODING_AGENT_DIR:-$HOME/.pi-agent}"
PI="$HOME/pi/pi"
[ -x "$PI" ] || PI=pi
rc=0
flock -w "${LOCK_WAIT:-1800}" -E 75 "$HOME/.ai-review-model.lock" python3 -I "$HERE/review.py" --repo "$REPO" --pr "$PR" \
  --workdir "$WORKDIR" --pi "$PI" --model "${MODEL:-zai/glm-5.3-flash}" --thinking "${THINKING:-medium}" \
  --parallel "${PARALLEL:-1}" --batch-chars "${BATCH_CHARS:-120000}" \
  --time-budget "${TIME_BUDGET:-15}" --mode "${MODE:-feature}" || rc=$?
if [ "$rc" -eq 75 ]; then
  echo "::warning::AI review skipped: the model was busy for ${LOCK_WAIT:-1800} seconds"
  gh api -X POST "repos/$REPO/issues/$PR/comments" -f body='The AI review was skipped because the model was busy. Add the `ai-review` label to try again.' >/dev/null || true
  exit 0
fi
exit "$rc"
