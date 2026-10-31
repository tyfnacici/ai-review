#!/bin/sh
# Runner-side entrypoint: loads the GLM Coding Plan key from the runner user's home
# and reviews one PR. Called by .github/workflows/review.yml.
set -eu
: "${GH_TOKEN:?}" "${REPO:?}" "${PR:?}" "${WORKDIR:?}"
HERE=$(cd "$(dirname "$0")" && pwd)
set -a
. "$HOME/.config/glm/zai.env"
set +a
export PI_CODING_AGENT_DIR="$HOME/.pi-agent"
exec python3 -I "$HERE/review.py" --repo "$REPO" --pr "$PR" --workdir "$WORKDIR" \
  --system "$HERE/system.md" --pi "$HOME/pi/pi" --model "${MODEL:-zai/glm-5.3}"
