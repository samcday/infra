export ETCDCTL_KEY=/var/lib/etcd/peer-key.pem
export ETCDCTL_CERT=/var/lib/etcd/peer.pem
export ETCDCTL_CACERT=/var/lib/etcd/ca.pem
export ETCDCTL_DISCOVERY_SRV=etcd.hub.internal

alias etcdctl='sudo -E podman exec -it --env=ETCDCTL_* etcd etcdctl'
