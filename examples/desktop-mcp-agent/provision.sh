#!/bin/sh
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

# Keep the agent source provider-neutral. Explicit model and endpoint settings
# override these defaults; DeepSeek endpoints always use DEEPSEEK_API_KEY.
OPENAI_MODEL=${OPENAI_MODEL:-deepseek-flash}
OPENAI_BASE_URL=${OPENAI_BASE_URL:-https://api.deepseek.com}
case "$OPENAI_BASE_URL" in
  https://api.deepseek.com|https://api.deepseek.com/*)
    # The host may also have an unrelated OPENAI_API_KEY. DeepSeek must use
    # its own credential even when that variable is already populated.
    OPENAI_API_KEY=${DEEPSEEK_API_KEY:-}
    if [ -z "$OPENAI_API_KEY" ]; then
      printf '%s\n' 'Set DEEPSEEK_API_KEY before provisioning.' >&2
      exit 2
    fi
    ;;
  *) OPENAI_API_KEY=${OPENAI_API_KEY:-} ;;
esac
if [ -z "${TYPESAFE_AI_KEY:-}" ]; then
  printf '%s\n' 'Set TYPESAFE_AI_KEY before provisioning for Jev browser routing.' >&2
  exit 2
fi
export OPENAI_MODEL OPENAI_BASE_URL OPENAI_API_KEY

exec python3 "$here/provision.py" "$@"
