#!/bin/bash
set -euo pipefail

die() {
  printf 'bootie-cluster: %s\n' "$*" >&2
  exit 1
}

for name in BOOTIE_HOST_IP BOOTIE_NODE_INVENTORY_FILE \
  BOOTIE_IMAGE_DIGEST BOOTIE_IMAGE_REVISION FCOS_VERSION; do
  [[ -n ${!name:-} ]] || die "$name is required"
done
[[ $BOOTIE_HOST_IP =~ ^10\.66\.1\.(10|11)$ ]] ||
  die 'BOOTIE_HOST_IP must be one Fabric service-node address'
[[ $BOOTIE_IMAGE_DIGEST =~ ^sha256:[0-9a-f]{64}$ ]] ||
  die 'BOOTIE_IMAGE_DIGEST must be one pinned OCI manifest digest'
[[ $BOOTIE_IMAGE_REVISION =~ ^[0-9a-f]{40}$ ]] ||
  die 'BOOTIE_IMAGE_REVISION must be one full Git commit'
[[ $FCOS_VERSION =~ ^[0-9][0-9A-Za-z.-]+$ ]] || die 'FCOS_VERSION is malformed'
[[ -f $BOOTIE_NODE_INVENTORY_FILE && ! -L $BOOTIE_NODE_INVENTORY_FILE ]] ||
  die 'the exact node inventory file is unavailable'

mapfile -t nodes < <(awk 'NF && $1 !~ /^#/ {print}' "$BOOTIE_NODE_INVENTORY_FILE")
((${#nodes[@]} == 5)) || die 'the PXE inventory must contain exactly five nodes'

declare -A seen_nodes=() seen_macs=() seen_addresses=()
dnsmasq_config=/run/bootie/dnsmasq.conf
install -d -o root -g nginx -m 2770 /run/bootie
install -d -m 0755 /run/bootie/tftp/EFI/BOOT
{
  cat <<EOF
port=0
user=root
group=root
interface=enp0s31f6
bind-interfaces
pid-file=/run/bootie/dnsmasq.pid
dhcp-leasefile=/run/bootie/dnsmasq.leases
dhcp-authoritative
dhcp-range=10.66.1.101,10.66.1.105,255.255.255.0,15m
dhcp-ignore=tag:!fabric-node
dhcp-option=tag:fabric-node,option:router
dhcp-option=tag:fabric-node,option:dns-server
enable-tftp
tftp-root=/run/bootie/tftp
dhcp-boot=tag:fabric-node,EFI/BOOT/BOOTX64.EFI,,$BOOTIE_HOST_IP
log-dhcp
log-facility=-
EOF
  for record in "${nodes[@]}"; do
    read -r node role address mac disk pxe_address extra <<<"$record"
    [[ -z ${extra:-} && $node =~ ^fabric-az1-(cp[123]|svc[12])$ &&
       $role =~ ^(control-plane|service)$ &&
       $address =~ ^10\.66\.[01]\.[0-9]{1,3}$ &&
       $mac =~ ^([0-9a-f]{2}:){5}[0-9a-f]{2}$ &&
       $disk == /dev/disk/by-id/* &&
       $pxe_address =~ ^10\.66\.1\.10[1-5]$ ]] ||
      die "malformed PXE inventory record for ${node:-unknown}"
    [[ -z ${seen_nodes[$node]:-} && -z ${seen_macs[$mac]:-} &&
       -z ${seen_addresses[$pxe_address]:-} ]] ||
      die 'PXE inventory contains a duplicate node, MAC, or lease address'
    seen_nodes[$node]=1
    seen_macs[$mac]=1
    seen_addresses[$pxe_address]=1
    printf 'dhcp-host=%s,set:fabric-node,%s,%s,infinite\n' \
      "$mac" "$pxe_address" "$node"
  done
} >"$dnsmasq_config"
chmod 0600 "$dnsmasq_config"

cp -a /usr/share/bootie/tftp/EFI/BOOT/. /run/bootie/tftp/EFI/BOOT/
install -m 0644 /usr/share/bootie/grub.cfg /run/bootie/tftp/EFI/BOOT/grub.cfg
dnsmasq --test --conf-file="$dnsmasq_config"
while read -r expected logical extra; do
  [[ $expected =~ ^[0-9a-f]{64}$ && -n $logical && -z ${extra:-} ]] ||
    die 'the embedded FCOS checksum manifest is malformed'
  case $logical in
    static/*) asset=/pxe/${logical#static/} ;;
    tftp/*) asset=/usr/share/bootie/tftp/EFI/BOOT/${logical#tftp/} ;;
    *) die 'the embedded FCOS checksum manifest names an unexpected path' ;;
  esac
  [[ -f $asset && ! -L $asset &&
     $(sha256sum "$asset" | awk '{print $1}') == "$expected" ]] ||
    die "embedded FCOS asset failed verification: $logical"
done </usr/share/bootie/FCOS-SHA256SUMS

# DHCP names the nested shim, but Fedora's shim requests its signed peers at
# the TFTP root. GRUB itself first probes beside the nested shim for grub.cfg.
# Keep both layouts: this is the same path behavior used by the attended
# staging helpers and is required by the physical Lenovo UEFI implementation.
for peer in grubx64.efi mmx64.efi; do
  install -m 0644 "/usr/share/bootie/tftp/EFI/BOOT/$peer" \
    "/run/bootie/tftp/$peer"
done
install -m 0644 /usr/share/bootie/grub.cfg /run/bootie/tftp/grub.cfg
for peer in grubx64.efi mmx64.efi; do
  cmp -s "/usr/share/bootie/tftp/EFI/BOOT/$peer" "/run/bootie/tftp/$peer" ||
    die "TFTP root peer differs from its verified embedded asset: $peer"
done
cmp -s /usr/share/bootie/grub.cfg /run/bootie/tftp/grub.cfg ||
  die 'TFTP root GRUB configuration differs from its embedded source'

if [[ ${BOOTIE_CLUSTER_CHECK:-false} == true ]]; then
  printf 'validated five-node PXE inventory, dnsmasq configuration, and embedded FCOS assets\n'
  exit 0
fi

# The two existing service nodes predate the permanent Bootie admissions in
# their Ignition. Converge the same narrow rules in the host network namespace
# so the first reimage does not require a direct host-firewall mutation. A
# reimaged node recreates the table with these rules already present.
firewall_chain=(inet fabric_services_guard input)
nft list chain "${firewall_chain[@]}" >/dev/null 2>&1 ||
  die 'the service-node host firewall chain is unavailable'
install_firewall_rule() {
  local comment=$1
  shift
  if ! nft -a list chain "${firewall_chain[@]}" |
    grep -F "comment \"$comment\"" >/dev/null; then
    nft insert rule "${firewall_chain[@]}" "$@" counter accept comment "$comment"
  fi
}
install_firewall_rule fabric-bootie-dhcp \
  iifname enp0s31f6 udp sport 68 udp dport 67
install_firewall_rule fabric-bootie-tftp \
  ip saddr '{ 10.66.1.101, 10.66.1.102, 10.66.1.103, 10.66.1.104, 10.66.1.105 }' \
  udp dport 69
install_firewall_rule fabric-bootie-http \
  ip saddr '{ 10.66.1.101, 10.66.1.102, 10.66.1.103, 10.66.1.104, 10.66.1.105 }' \
  tcp dport 80
for comment in fabric-bootie-dhcp fabric-bootie-tftp fabric-bootie-http; do
  nft -a list chain "${firewall_chain[@]}" |
    grep -F "comment \"$comment\"" >/dev/null ||
    die "host firewall admission is absent: $comment"
done

export BOOTIE_DAY2_MODE=true
export BOOTIE_PUBLIC_ORIGIN="http://$BOOTIE_HOST_IP"
export FCOS_BASE="http://$BOOTIE_HOST_IP/static"
export FCOS_GRUB_BASE="(http,$BOOTIE_HOST_IP)/static"

fcgi_socket=/run/bootie/fcgi.sock
[[ ! -e $fcgi_socket && ! -L $fcgi_socket ]] ||
  die 'the FastCGI Unix socket path is unexpectedly occupied'
(
  umask 0007
  exec fcgiwrap -s "unix:$fcgi_socket"
) &
fcgi_pid=$!
for _ in {1..50}; do
  [[ -S $fcgi_socket && ! -L $fcgi_socket ]] && break
  kill -0 "$fcgi_pid" 2>/dev/null || {
    wait "$fcgi_pid" || true
    die 'fcgiwrap exited before creating its Unix socket'
  }
  sleep 0.1
done
[[ -S $fcgi_socket && ! -L $fcgi_socket ]] ||
  die 'fcgiwrap did not create its Unix socket'
chown root:nginx "$fcgi_socket"
chmod 0770 "$fcgi_socket"
dnsmasq --keep-in-foreground --conf-file="$dnsmasq_config" &
dnsmasq_pid=$!

# Invoked through the signal/exit trap below.
# shellcheck disable=SC2329
cleanup() {
  kill "$fcgi_pid" "$dnsmasq_pid" "${nginx_pid:-}" 2>/dev/null || true
  wait "$fcgi_pid" "$dnsmasq_pid" "${nginx_pid:-}" 2>/dev/null || true
}
trap cleanup EXIT INT HUP TERM

nginx &
nginx_pid=$!
wait -n "$fcgi_pid" "$dnsmasq_pid" "$nginx_pid"
die 'a Bootie cluster process exited unexpectedly'
