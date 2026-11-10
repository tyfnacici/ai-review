#!/bin/bash
# Sets up a self-hosted GitHub Actions runner for ai-review on a Linux machine (x64 or arm64).
# Run as a user with sudo and an authenticated gh (repo admin):
#   ./install.sh owner/repo            # runner for one repository
#   ./install.sh my-org                # runner for every repository of an organization
# Environment overrides: LABEL, RUNNER_NAME, KEY_FILE, AI_REVIEW_URL, RUNNER_USER, RUNNER_HOME.
set -euo pipefail

TARGET=${1:?usage: install.sh owner/repo | org}
LABEL=${LABEL:-ai-review}
RUNNER_NAME=${RUNNER_NAME:-$(hostname)-ai-review}
AI_REVIEW_URL=${AI_REVIEW_URL:-https://github.com/tyfnacici/ai-review.git}
U=${RUNNER_USER:-ai-review}
H=${RUNNER_HOME:-/opt/ai-review}
RDIR=$H/runners/${TARGET//\//-}

case $(uname -m) in
  x86_64) ARCH=x64 ;;
  aarch64|arm64) ARCH=arm64 ;;
  *) echo "unsupported architecture: $(uname -m)"; exit 1 ;;
esac
case $TARGET in
  */*) API=repos/$TARGET ;;
  *) API=orgs/$TARGET ;;
esac

step() { printf '\n== %s\n' "$*"; }
as_u() { sudo -u "$U" -H bash -c 'cd "$HOME" && exec "$@"' _ "$@"; }
fetch() {
  local tmp
  tmp=$(mktemp -d)
  curl -fsSL -o "$tmp/f.tgz" "$1"
  echo "$2  $tmp/f.tgz" | sha256sum -c - >/dev/null
  sudo install -d -o "$U" -g "$U" -m 750 "$3"
  sudo tar xzf "$tmp/f.tgz" -C "$3"
  sudo chown -R "$U:$U" "$3"
  rm -rf "$tmp"
}

step "1/6 Requirements"
for cmd in git gh python3 curl tar sha256sum sudo; do
  command -v "$cmd" >/dev/null || { echo "missing: $cmd"; exit 1; }
done
gh auth status >/dev/null

step "2/6 User $U ($H)"
id "$U" >/dev/null 2>&1 || sudo useradd --system --create-home --home-dir "$H" --shell /bin/bash "$U"
sudo chmod 750 "$H"

step "3/6 Reviewer code"
if sudo test -d "$H/ai-review/.git"; then
  as_u git -C "$H/ai-review" fetch -q origin main
  as_u git -C "$H/ai-review" reset -q --hard origin/main
else
  as_u git clone -q "$AI_REVIEW_URL" "$H/ai-review"
fi
as_u git -C "$H/ai-review" log --oneline -1

step "4/6 Pi (latest) and API key file"
as_u "$H/ai-review/update-pi.sh"
sudo install -d -o "$U" -g "$U" -m 700 "$H/.config" "$H/.config/ai-review" "$H/.pi-agent"
if [ -n "${KEY_FILE:-}" ]; then
  sudo install -o "$U" -g "$U" -m 600 "$KEY_FILE" "$H/.config/ai-review/env"
elif ! sudo test -f "$H/.config/ai-review/env"; then
  printf '# API key for the provider of the review model, e.g.\n# ZAI_API_KEY=...\n# ANTHROPIC_API_KEY=...\n' \
    | sudo -u "$U" tee "$H/.config/ai-review/env" >/dev/null
  sudo chmod 600 "$H/.config/ai-review/env"
fi
as_u "$H/pi/pi" --version

step "5/6 Runner ($TARGET, label $LABEL)"
if ! sudo test -f "$RDIR/config.sh"; then
  body=$(gh api repos/actions/runner/releases/latest --jq '.tag_name + "\n" + .body')
  RUNNER_VER=$(echo "$body" | head -1 | sed 's/^v//')
  RUNNER_SHA=$(echo "$body" | sed -n "s/.*BEGIN SHA linux-$ARCH -->\([0-9a-f]\{64\}\)<.*/\1/p")
  fetch "https://github.com/actions/runner/releases/download/v$RUNNER_VER/actions-runner-linux-$ARCH-$RUNNER_VER.tar.gz" \
    "$RUNNER_SHA" "$RDIR"
  sudo "$RDIR/bin/installdependencies.sh" >/dev/null
fi
if ! sudo test -f "$RDIR/.runner"; then
  token=$(gh api -X POST "$API/actions/runners/registration-token" --jq .token)
  as_u env T="$token" bash -c "cd '$RDIR' && ./config.sh --unattended --url 'https://github.com/$TARGET' \
    --token \"\$T\" --name '$RUNNER_NAME' --labels '$LABEL' --work _work --replace"
  unset token
fi

step "6/6 Service"
sudo bash -c "cd '$RDIR' && { test -f .service || ./svc.sh install '$U' >/dev/null; } && ./svc.sh start >/dev/null"
gh api "$API/actions/runners" --jq '.runners[] | "runner: \(.name) \(.status) [\(.labels | map(.name) | join(","))]"'

sudo grep -q '^[A-Z_]*_API_KEY=.' "$H/.config/ai-review/env" || cat <<EOF

Next: put the model provider's API key into $H/.config/ai-review/env, e.g.
  sudo -u $U nano $H/.config/ai-review/env
EOF
echo
echo "Done. In the calling workflow set: runs_on: '[\"self-hosted\", \"$LABEL\"]'"
