#!/bin/sh
# Installs or updates the Pi CLI in $HOME/pi to its latest release (SHA-256 checked); the previous one stays in $HOME/pi.old.
set -eu
DIR=${PI_DIR:-$HOME/pi}
URL=https://github.com/badlogic/pi-mono/releases
case $(uname -m) in
  x86_64) ASSET=pi-linux-x64.tar.gz ;;
  aarch64|arm64) ASSET=pi-linux-arm64.tar.gz ;;
  *) echo "unsupported architecture: $(uname -m)"; exit 1 ;;
esac
latest=$(curl -fsSLI -o /dev/null -w '%{url_effective}' "$URL/latest")
latest=${latest##*/v}
current=$("$DIR/pi" --version 2>/dev/null || true)
[ "$current" = "$latest" ] && exit 0
tmp=$(mktemp -d "$DIR.new.XXXXXX")
trap 'rm -rf "$tmp"' EXIT
curl -fsSL -o "$tmp/pi.tgz" "$URL/download/v$latest/$ASSET"
sha=$(curl -fsSL "$URL/download/v$latest/SHA256SUMS" | awk -v f="$ASSET" '$2 == f {print $1}')
echo "$sha  $tmp/pi.tgz" | sha256sum -c - >/dev/null
mkdir "$tmp/pi"
tar xzf "$tmp/pi.tgz" -C "$tmp/pi" --strip-components=1
[ "$("$tmp/pi/pi" --version)" = "$latest" ]
rm -rf "$DIR.old"
if [ -d "$DIR" ]; then mv "$DIR" "$DIR.old"; fi
mv "$tmp/pi" "$DIR"
echo "pi ${current:-none} -> $latest"
