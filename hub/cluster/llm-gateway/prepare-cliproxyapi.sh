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

# In the C locale [:graph:] is exactly ASCII ! through ~. MiMo tokens may
# contain punctuation; only YAML double-quote/backslash escapes are forbidden.
# Validate both secrets before reading or writing any runtime configuration.
LC_ALL=C
export LC_ALL
case "${MIMO_CODE_API_KEY:-}" in
    ''|*[![:graph:]]*|*'"'*|*'\'*)
        echo "MIMO_CODE_API_KEY must be nonempty ASCII graph without double quotes or backslashes" >&2
        exit 1
        ;;
esac
case "$MIMO_CODE_API_KEY" in
    *__MIMO_CODE_API_KEY__*|@*@|*@@*@@*)
        echo "MIMO_CODE_API_KEY must be a resolved credential, not a placeholder" >&2
        exit 1
        ;;
esac

input=${1:-/config/config.yaml}
output=${2:-/runtime/config.yaml}

# Keep the template in memory, including its final newline (if present).
config=
line=
while IFS= read -r line; do
    config=$config$line'
'
done < "$input"
config=$config$line

bad_placeholder() {
    echo "config requires exactly one double-quoted MiMo placeholder, with none left unresolved" >&2
    exit 1
}

# Require exactly one occurrence, as an entire double-quoted YAML scalar.
# Parameter expansion and printf never interpret token text as shell code,
# replacement syntax, a format string or YAML escapes.
placeholder=__MIMO_CODE_API_KEY__
case "$config" in *"$placeholder"*) ;; *) bad_placeholder ;; esac
before=${config%%"$placeholder"*}
after=${config#*"$placeholder"}
case "$after" in *"$placeholder"*) bad_placeholder ;; esac
case "$before" in *'"') ;; *) bad_placeholder ;; esac
case "$after" in '"'*) ;; *) bad_placeholder ;; esac
rendered=$before$MIMO_CODE_API_KEY$after
case "$rendered" in *"$placeholder"*) bad_placeholder ;; esac
case "$CLIPROXY_API_KEY" in *"$placeholder"*) bad_placeholder ;; esac

# A retried init may find an existing file. Restrict it before writing secrets;
# new files are created privately by umask above.
if [ -e "$output" ]; then
    chmod 600 "$output"
fi
{
    printf '%s' "$rendered"
    printf '\naccess:\n  api-keys:\n    - "%s"\n' "$CLIPROXY_API_KEY"
} > "$output"
