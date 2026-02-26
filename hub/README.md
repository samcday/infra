# hub

hub.

## Kubeconfig Sync

Build/update a local OIDC kubeconfig for managed clusters (from repo root):

```bash
./scripts/sync-infra-kubeconfig.sh
```

The script uses `HUB_CONTEXT` if set, otherwise it tries context `default`.

Install a user cron job to keep it fresh every 15 minutes:

```bash
(crontab -l 2>/dev/null; echo '*/15 * * * * PATH=/usr/local/bin:/usr/bin:/bin /var/home/sam/src/infra/scripts/sync-infra-kubeconfig.sh >/dev/null 2>&1') | crontab -
```

If `crontab` is unavailable, use the user timer units in `~/.config/systemd/user/infra-kubeconfig-sync.{service,timer}` and run:

```bash
systemctl --user daemon-reload
systemctl --user enable --now infra-kubeconfig-sync.timer
```

Then use both configs together:

```bash
export KUBECONFIG="$HOME/.kube/config:$HOME/.kube/infra.generated"
```

Quick one-off merge example (same idea as `kubectl get ... > /tmp/foo && kubectl config flatten && mv`):

```bash
kubectl -n cloud-cluster get secret admin-kubeconfig-external -o jsonpath='{.data.value}' | base64 -d > /tmp/cloud.kubeconfig \
  && KUBECONFIG="$HOME/.kube/config:/tmp/cloud.kubeconfig" kubectl config view --flatten > /tmp/kubeconfig \
  && mv /tmp/kubeconfig "$HOME/.kube/config"
```

## Bootstrappin'

 * Make image and flash [router](./router/README.md)
 * Generate [Ignition](./ignition):
   From `butane/` dir: `./bootstrap.sh > ~/Downloads/config.ign && podman run -v $HOME/Downloads:/dl --rm quay.io/coreos/coreos-installer:release iso customize --dest-device=/dev/sda --dest-ignition /dl/config.ign -f -o /dl/custom.iso '/dl/fedora-coreos-*-live-iso.x86_64.iso'` or smth idk
 * Start up a node with customized ISO.
 * Start up more once bootie is running.

### Pet etcd cluster

SSH into a node, run:

```
# create root + hub control-plane users
etcdctl user add root --no-password
etcdctl user add hub-cp --no-password

# enable RBAC
etcdctl auth enable

# add hub-cp role
etcdctl role add hub-cp
etcdctl user grant-role hub-cp hub-cp
etcdctl role grant-permission hub-cp --prefix=true readwrite /hub-cp/
etcdctl role grant-permission hub-cp --prefix=true readwrite /bootstrap/
```
