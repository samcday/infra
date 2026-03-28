#!/bin/bash

set -uexo pipefail

if [[ -d /butane ]]; then
    for f in /butane/*.yaml; do
        name=$(basename "$f")
        butane --strict < "$f" > "/ignition/${name/.yaml}".ign
    done
fi

fcgiwrap -s 'tcp:0.0.0.0:9000' &
nginx
