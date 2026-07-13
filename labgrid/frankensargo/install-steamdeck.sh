#!/bin/bash
set -euo pipefail

if [[ "$(id -un)" != "deck" ]]; then
  echo "run this installer as the Steam Deck's deck user" >&2
  exit 1
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
image="docker.io/labgrid/exporter:v26.0@sha256:80ccee11dd4780fb1dfad5bbab93013ab00323a8d0c3905b370e9283fffe2749"

install -d -m 0755 "${HOME}/.config/labgrid" "${HOME}/.config/systemd/user"
install -m 0644 "${script_dir}/exporter.yaml" "${HOME}/.config/labgrid/frankensargo.yaml"
install -m 0644 \
  "${script_dir}/frankensargo-labgrid-exporter.service" \
  "${HOME}/.config/systemd/user/frankensargo-labgrid-exporter.service"

sudo install -d -o deck -g uucp -m 2770 /var/cache/labgrid
sudo install -m 0644 \
  "${script_dir}/99-labgrid-frankensargo.rules" \
  /etc/udev/rules.d/99-labgrid-frankensargo.rules
sudo udevadm control --reload-rules
sudo loginctl enable-linger deck

podman pull "${image}"
systemctl --user daemon-reload
systemctl --user enable --now frankensargo-labgrid-exporter.service
