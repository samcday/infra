#!/bin/sh
set -eu
umask 077

# This is our generated gateway-to-proxy key, not an upstream provider token.
# Restrict its alphabet so inserting it as a YAML scalar cannot change config.
case "${CLIPROXY_API_KEY:-}" in
    ''|*[!A-Za-z0-9_-]*)
        echo "CLIPROXY_API_KEY must be a nonempty URL-safe token" >&2
        exit 1
        ;;
esac

cp /config/config.yaml /runtime/config.yaml
printf '\naccess:\n  api-keys:\n    - "%s"\n' "$CLIPROXY_API_KEY" >> /runtime/config.yaml
