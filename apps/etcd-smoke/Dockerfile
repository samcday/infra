FROM quay.io/coreos/etcd:v3.6.13@sha256:f5a43614f4a7c74891b9fc3aecd27197af896911e24a203046acd094dd34f8bc AS etcd

FROM docker.io/library/busybox:1.37.0@sha256:9532d8c39891ca2ecde4d30d7710e01fb739c87a8b9299685c63704296b16028

COPY --from=etcd /usr/local/bin/etcdctl /usr/local/bin/etcdctl

USER 65534:65534
WORKDIR /work
