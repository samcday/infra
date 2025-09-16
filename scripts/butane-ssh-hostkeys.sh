#!/bin/bash
set -uexo pipefail

cd $(mktemp -d)

ssh-keygen -q -t ed25519 -C '' -N '' -f ssh_host_ed25519_key
ssh-keygen -q -t ecdsa -b 521 -C '' -N '' -f ssh_host_ecdsa_key
ssh-keygen -q -t rsa -b 4096 -C '' -N '' -f ssh_host_rsa_key

cat <<HERE
variant: fcos
version: 1.5.0
storage:
    files:
HERE
for f in *; do
    echo "    - path: /etc/ssh/$f"
    echo "      overwrite: true"
    echo "      mode: 0600"
    echo "      contents:"
    if [[ "$f" != *.pub ]]; then
        echo "        # cryptme"
    fi
    echo "        inline: |"
    sed 's/^/          /' "$f"
done
