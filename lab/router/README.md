# Lab router

The Lab router is an AVM FRITZ!Box 4040 with wired DHCP WAN and the isolated
`10.0.4.0/24` worker LAN. It provides DHCP/TFTP/iPXE, router-local FCOS and
system-extension assets, Tang, and the pre-OS Tailnet hop to Fabric Bootie.

Build the initial EVA and later sysupgrade images from the repository root:

```sh
scripts/build-router-image.sh lab/router ipq40xx generic avm_fritzbox-4040
```

The router is registered interactively after first boot with
`/root/register-tailnet`; no reusable Headscale credential is embedded in its
image. Wi-Fi and Tailnet subnet advertisement are deliberately disabled.

Build the worker system extension and an independently checksummed USB tree:

```sh
scripts/build-lab-node-sysext --check
scripts/prepare-lab-router-data
```

Format the router USB filesystem as ext4 with label `data`, then copy the
generated `_build/lab-router-data/www/` directory to the filesystem root.
The image mounts that filesystem at `/mnt/data` and exposes only `www/` at
`http://10.0.4.1/static/`.
