#!/bin/bash

set -uexo pipefail

if [[ -d /butane ]]; then
    for f in /butane/*.yaml; do
        name=$(basename "$f")
        butane --strict < "$f" > "/ignition/${name/.yaml}".ign
    done
fi

fcgiwrap -s 'tcp:127.0.0.1:9000' &
exec nginx
