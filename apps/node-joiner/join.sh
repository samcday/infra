#!/bin/bash
set -euo pipefail

: "${NODE_NAME:?must be set}"
: "${NODE_IP:?must be set}"
: "${HEADSCALE_URL:?must be set}"
: "${APISERVER_URL:?must be set}"
: "${TS_AUTH_KEY:?must be set}"
: "${BOOTSTRAP_TOKEN:?must be set}"
: "${CA_HASH:?must be set}"

NODE_PASSWORD=$(kubectl --kubeconfig=/etc/edge-kubeconfig/kubeconfig \
  -n kube-system get secret "$NODE_NAME-node-password" \
  -o jsonpath='{.data.password}' | base64 -d)

SSH_OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10"

until sshpass -p "$NODE_PASSWORD" ssh $SSH_OPTS "root@$NODE_IP" true; do
  echo "waiting for sshd on $NODE_IP..."
  sleep 10
done

APISERVER_HOST=${APISERVER_URL%%:*}

sshpass -p "$NODE_PASSWORD" ssh $SSH_OPTS "root@$NODE_IP" bash -s <<EOF
exec 2>&1
set -euo pipefail
# cloud-init exits 2 on "recoverable error" for cosmetic module warnings,
# which Ubuntu 24.04 reliably trips. Treat any exit as "wait complete".
cloud-init status --wait || true

tailscale up --login-server=${HEADSCALE_URL} --auth-key=${TS_AUTH_KEY}

# kubelet's --resolv-conf on this image points at /run/systemd/resolve/resolv.conf,
# which lists upstream DNS directly and bypasses systemd-resolved's per-domain
# routing to tailscale. Pods on the node then can't resolve the tailnet apiserver.
# Pin it in /etc/hosts so every resolver on the box sees the same IP.
APISERVER_IP=\$(getent hosts ${APISERVER_HOST} | awk '{print \$1; exit}')
echo "\${APISERVER_IP} ${APISERVER_HOST}" >> /etc/hosts

kubeadm join ${APISERVER_URL} --node-name ${NODE_NAME} --token ${BOOTSTRAP_TOKEN} --discovery-token-ca-cert-hash sha256:${CA_HASH}
EOF
