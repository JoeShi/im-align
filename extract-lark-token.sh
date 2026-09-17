#!/bin/bash
# Extract the Feishu/Lark user token from the local lark-cli credential store.
# Prints the token to stdout so it can be captured into an environment variable:
#
#   IM_ALIGN_E2E_USER_ACCESS_TOKEN="$(./extract-lark-token.sh)"
#   IM_ALIGN_E2E_FEISHU_USER_REFRESH_TOKEN="$(./extract-lark-token.sh --token refresh)"
#
# The store format (macOS, measured 2026-09-16) is documented in
# references/feishu-setup.md ("Extracting The Local Token From The lark-cli
# Credential Store"). Never commit the printed token or the store files.

set -euo pipefail

TOKEN_KIND=access
APP_ID=""
LIST_ONLY=0

while [ $# -gt 0 ]; do
  case "$1" in
    --token)
      TOKEN_KIND="${2:?--token requires access or refresh}"
      shift 2
      ;;
    --app)
      APP_ID="${2:?--app requires an app id like cli_...}"
      shift 2
      ;;
    --list)
      LIST_ONLY=1
      shift
      ;;
    -h|--help)
      sed -n '2,11p' "$0"
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

case "$TOKEN_KIND" in
  access|refresh) ;;
  *) echo "--token must be 'access' or 'refresh', got '$TOKEN_KIND'" >&2; exit 2 ;;
esac

STORE=""
for candidate in \
  "$HOME/Library/Application Support/lark-cli" \
  "${XDG_CONFIG_HOME:-$HOME/.config}/lark-cli"; do
  if [ -f "$candidate/master.key.file" ]; then
    STORE="$candidate"
    break
  fi
done
if [ -z "$STORE" ]; then
  echo "lark-cli credential store not found (looked in macOS and XDG config paths)" >&2
  echo "complete the interactive grant first: lark-cli auth login" >&2
  exit 1
fi

PY_TOKEN_KIND="$TOKEN_KIND" PY_APP_ID="$APP_ID" PY_LIST_ONLY="$LIST_ONLY" \
STORE="$STORE" uv run --with cryptography --frozen python - <<'EOF'
import datetime
import json
import os
import pathlib
import sys

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

store = pathlib.Path(os.environ["STORE"])
token_kind = os.environ["PY_TOKEN_KIND"]
app_id = os.environ["PY_APP_ID"]
list_only = os.environ["PY_LIST_ONLY"] == "1"

key = AESGCM((store / "master.key.file").read_bytes())


def load(enc_path):
    raw = enc_path.read_bytes()
    return json.loads(key.decrypt(raw[:12], raw[12:], None))


enc_files = sorted(store.glob("cli_*.enc"))
if app_id:
    enc_files = [p for p in enc_files if p.name.startswith(app_id + "_")]
if not enc_files:
    print(f"no credential file found in {store} for app filter {app_id or '<any>'}", file=sys.stderr)
    sys.exit(1)

entries = [(p, load(p)) for p in enc_files]

if list_only:
    for p, data in entries:
        def ts(field):
            return datetime.datetime.fromtimestamp(int(data[field]) / 1000).strftime("%Y-%m-%d %H:%M:%S")
        print(f"{p.name}")
        print(f"  appId: {data['appId']}  userOpenId: {data['userOpenId']}")
        print(f"  accessToken expires:     {ts('expiresAt')}")
        print(f"  refreshToken expires:   {ts('refreshExpiresAt')}")
    sys.exit(0)

if len(entries) > 1:
    print("multiple credentials found; narrow with --app <appId>:", file=sys.stderr)
    for p, data in entries:
        print(f"  {p.name}", file=sys.stderr)
    sys.exit(1)

data = entries[0][1]
token = data["accessToken" if token_kind == "access" else "refreshToken"]
expiry = int(data["expiresAt" if token_kind == "access" else "refreshExpiresAt"]) / 1000
if expiry < datetime.datetime.now().timestamp():
    print(
        f"warning: {token_kind} token expired at "
        f"{datetime.datetime.fromtimestamp(expiry).strftime('%Y-%m-%d %H:%M:%S')}; "
        "re-authorize with: lark-cli auth login",
        file=sys.stderr,
    )
print(token)
EOF
