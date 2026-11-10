#!/bin/sh
# Runner-side entrypoint: loads the model API key from the runner user's home and reviews one PR.
# Called by .github/workflows/review.yml.
set -eu
: "${GH_TOKEN:?}" "${REPO:?}" "${PR:?}" "${WORKDIR:?}"
HERE=$(cd "$(dirname "$0")" && pwd)
set -a
for f in "$HOME/.config/glm/zai.env" "$HOME/.config/ai-review/env"; do
  if [ -f "$f" ]; then . "$f"; fi
done
set +a
export PI_CODING_AGENT_DIR="${PI_CODING_AGENT_DIR:-$HOME/.pi-agent}"
PI="$HOME/pi/pi"
[ -x "$PI" ] || PI=pi
exec python3 -I "$HERE/review.py" --repo "$REPO" --pr "$PR" --workdir "$WORKDIR" \
  --pi "$PI" --model "${MODEL:-zai/glm-5.3}" --thinking "${THINKING:-high}" \
  --parallel "${PARALLEL:-3}" --batch-chars "${BATCH_CHARS:-120000}" --mode "${MODE:-feature}"
