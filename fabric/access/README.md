# Fabric operator access through the Tailnet

Two userspace Tailscale subnet routers on the Fabric service nodes advertise
the exact root and service prefixes. The tracked kubeconfig connects directly
to the API VIP at `10.66.0.254:6443`; root-node SSH uses those same routes.
No Tailscale process or credential is installed on a consensus root, and no
K3s administrator kubeconfig or CA private key leaves one.

The access path retains independent trust boundaries:

1. Headscale ACLs and route approval admit Sam to the two exact advertised
   prefixes;
2. default subnet-route SNAT presents one of the reviewed service-node source
   addresses to Fabric; and
3. each destination's host policy, API certificate, or pinned SSH host key
   remains authoritative for the requested port and identity.

Kubernetes TLS remains end to end across the Tailnet route. Although the
kubeconfig connects to the numeric VIP, it verifies `api.fabric.internal`
against the pinned `server-ca.crt`.

## Requirements

- the local Tailscale client is connected to Headscale and accepts subnet
  routes;
- at least one enrolled Fabric subnet-router Pod is Ready;
- the router and root-host Tailnet TCP/22 admissions are live;
- the caller has Sam's SSH identity, defaulting to `~/.ssh/id_ed25519`;
- the commissioned public keys in `known_hosts` still match; and
- the client has Bash, jq, OpenSSL, OpenSSH, and kubectl.

OpenSSH user configuration and connection sharing are ignored so an existing
master, jump host, or proxy command cannot bypass the direct pinned path. Set
`FABRIC_SSH_IDENTITY` when the private key is at a different path.

An installation carrying the earlier observer-only policy cannot bootstrap
this cutover through direct root SSH: both the router and root host reject the
service-node SNAT sources. Stage the committed
`Allow-tailnet-SNAT-to-root-SSH` router rule and `tailnet-routed operator SSH`
root-host rule once through an attended console or reprovisioning path. After
that migration, routine access has no observer-namespace dependency.

## Use it

The tracked kubeconfig defaults to fabric, so this works from any directory on
a Tailnet machine whose checkout is at `~/src/infra`:

```sh
kubectl --kubeconfig ~/src/infra/kubeconfig get nodes -o wide
kubectl --kubeconfig ~/src/infra/kubeconfig get --raw=/readyz
```

The repository wrappers remain convenient:

```sh
scripts/fk get pods -A
scripts/fk k9s
scripts/ik --context=fabric get nodes
```

`fabric-credential` generates a P-256 key locally. Over direct pinned SSH, a
root validates the exact
`O=system:masters,CN=sam-fabric-operator` CSR, constrains its key and
extensions, and signs it directly with the K3s client CA for about 15 minutes
of usable time. A one-minute `notBefore` backdate makes the encoded validity
window at most 16 minutes while tolerating small client/root clock skew.
The CA key stays on the root, and no CSR or RBAC object is created in
Kubernetes. The mode-0600 key and leaf live only in the caller's protected
runtime directory and rotate with at least three minutes remaining. This
leaves one minute of slack above the two-minute minimum required by attended
fabric qualification helpers.

Native API TLS means watches, exec streams, k9s, and concurrent clients use the
Tailnet route directly without a local SSH tunnel or `kubectl proxy`.

Connect to a root for a version-matched emergency CLI:

```sh
scripts/fabric-ssh cp2 sudo k3s kubectl get nodes -o wide
```

## Boundary

The Tailnet path deliberately terminates on the two service nodes, whose
separate identities provide failover while keeping the consensus roots free of
Tailnet state. The pinned API trust, strict node host keys, destination host
guards, and short-lived operator credential remain independent controls.
