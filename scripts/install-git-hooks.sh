#!/bin/sh
# Install a pre-commit hook that refuses commits containing API keys.
# Run once per clone: sh scripts/install-git-hooks.sh
set -e
root=$(git rev-parse --show-toplevel)
hook="$root/.git/hooks/pre-commit"
cat > "$hook" <<'EOF'
#!/bin/sh
# Installed by scripts/install-git-hooks.sh: block commits that contain keys.
root=$(git rev-parse --show-toplevel)
python3 "$root/scripts/check_secrets.py" || {
  echo "Commit blocked. Remove the secret (keys belong in .env, which is git-ignored)." >&2
  exit 1
}
EOF
chmod +x "$hook"
echo "Installed $hook"
