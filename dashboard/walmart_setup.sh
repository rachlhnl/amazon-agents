#!/bin/zsh
# Connects the dashboard to the Walmart Marketplace API. This file holds no credentials.
#   zsh walmart_setup.sh keys    Saves the keys to .env (typing hidden)
#   zsh walmart_setup.sh status  Shows whether each key is filled in, never its value
#   zsh walmart_setup.sh test    Signs in and counts WFS items; changes nothing
# Keys: Seller Center -> https://seller.walmart.com/api-key -> API Key Management.
set -euo pipefail
HERE="${0:A:h}"
ENV_FILE="${ENV_FILE:-${HERE:h}/.env}"

show_status() {
  local key
  for key in WALMART_CLIENT_ID WALMART_CLIENT_SECRET; do
    if grep -Eq "^${key}=[^#[:space:]]+" "$ENV_FILE" 2>/dev/null; then
      print "  $key: set"
    else
      print "  $key: EMPTY"
    fi
  done
}

save_key() {
  local key=$1 label=$2 val="" tmp
  print -n "$label (hidden; Enter to skip): "
  read -rs val || val=""
  print
  val="${val//[[:space:]]/}"
  if [[ -z $val ]]; then
    print "  skipped"
    return 0
  fi
  touch "$ENV_FILE"
  tmp="$(mktemp "$ENV_FILE.XXXXXX")"
  KEY="$key" VAL="$val" awk '
    BEGIN { k = ENVIRON["KEY"]; v = ENVIRON["VAL"] }
    index($0, k "=") == 1 { print k "=" v; done = 1; next }
    { print }
    END { if (!done) print k "=" v }
  ' "$ENV_FILE" > "$tmp"
  chmod 600 "$tmp"
  mv -f "$tmp" "$ENV_FILE"
  print "  saved"
}

case "${1:-status}" in
  keys)
    print "Paste each value when asked. Nothing shows as you paste, and that's expected.\n"
    save_key WALMART_CLIENT_ID "Client ID"
    save_key WALMART_CLIENT_SECRET "Client Secret"
    print "\nCurrent status:"
    show_status
    ;;
  status) show_status ;;
  test) python3 -I "$HERE/walmart_api.py" check ;;
  *) print "Usage: zsh walmart_setup.sh keys|status|test"; exit 1 ;;
esac
