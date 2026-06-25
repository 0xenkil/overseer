#!/bin/sh
# Overseer one-line installer.
#   curl -fsSL https://raw.githubusercontent.com/0xenkil/overseer/main/install.sh | sh
# Then:  overseer setup
set -e

REPO="${OVERSEER_REPO:-https://github.com/0xenkil/overseer}"
DEST="${OVERSEER_HOME:-/opt/overseer}"
PY="$(command -v python3 || true)"

[ -n "$PY" ] || { echo "ERROR: python3 is required (it's the only dependency)."; exit 1; }

echo ">> Installing Overseer into $DEST"
if [ -d "$DEST/.git" ]; then
  git -C "$DEST" pull --ff-only
elif command -v git >/dev/null 2>&1; then
  git clone --depth 1 "$REPO" "$DEST"
else
  echo "git not found. Either install git, or copy this repo to $DEST manually, then re-run."
  exit 1
fi

# global launcher
cat > /usr/local/bin/overseer <<EOF
#!/bin/sh
exec env PYTHONPATH="$DEST" "$PY" -m overseer "\$@"
EOF
chmod +x /usr/local/bin/overseer

echo ""
echo ">> Installed. Next step:"
echo "     overseer setup"
echo ""
echo "   (you'll need an AI key - Gemini / Groq / Claude - and a Telegram bot token from @BotFather)"
