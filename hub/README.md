# hub

hub.

## Bootstrappin'

 * Make image and flash [router](./router/README.md)
 * Generate [Ignition](./ignition)
 * From `ignition/` dir: `./bootstrap.sh > ~/Downloads/config.ign | podman run -v $HOME/Downloads:/dl --rm quay.io/coreos/coreos-installer:release iso customize --dest-device=/dev/sda --dest-ignition /dl/config.ign -f -o /dl/custom.iso '/dl/fedora-coreos-*-live-iso.x86_64.iso'` or smth idk
 * Start up two nodes with bootstrapped ISO

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
