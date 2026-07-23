#!/bin/bash

set -uexo pipefail

if [[ -d /butane ]]; then
    for f in /butane/*.yaml; do
        name=$(basename "$f")
        butane --strict < "$f" > "/ignition/${name/.yaml}".ign
    done
fi

install -d -o root -g nginx -m 2770 /run/bootie
fcgi_socket=/run/bootie/fcgi.sock
[[ ! -e $fcgi_socket && ! -L $fcgi_socket ]]
(
    umask 0007
    exec fcgiwrap -s "unix:$fcgi_socket"
) &
fcgi_pid=$!
for _ in {1..50}; do
    [[ -S $fcgi_socket && ! -L $fcgi_socket ]] && break
    kill -0 "$fcgi_pid" 2>/dev/null || {
        wait "$fcgi_pid" || true
        printf 'bootie: fcgiwrap exited before creating its Unix socket\n' >&2
        exit 1
    }
    sleep 0.1
done
[[ -S $fcgi_socket && ! -L $fcgi_socket ]]
chown root:nginx "$fcgi_socket"
chmod 0770 "$fcgi_socket"
exec nginx
