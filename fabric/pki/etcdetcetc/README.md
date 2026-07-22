# Fabric etcdetcetc client PKI

This directory owns the public half and offline generation procedure for the
delegated CA used by cert-manager to issue etcd **client-only** certificates.
It is independent of the physical etcd CA. The physical CA key remains offline
and is never copied into Kubernetes.

The delegated CA is intentionally an online root-equivalent credential: etcd
maps a client certificate's common name to an authenticated user, so anyone
who can use this key can mint `root` or another privileged identity. Its SOPS
Secret is therefore isolated in the `cert-manager` namespace, where the
etcdetcetc ServiceAccount has no RBAC. The versioned
`fabric-etcd-client-v1` ClusterIssuer is usable only through fail-closed
ValidatingAdmissionPolicies: Flux may define the one admin leaf, the controller
may define UID-scoped tenant leaves, cert-manager may create only their owned
immutable requests, and all client leaves are fixed to the short-lived
client-only shape. Do not expose this key through an unrestricted
ClusterIssuer or move it into a controller-readable namespace.

Generate the initial CA only after reviewing the target paths:

```sh
./generate-client-ca --apply \
  --confirm GENERATE:fabric-etcdetcetc-client-ca
./generate-client-ca --check
./verify-foundation
```

Plaintext key material exists only inside a verified tmpfs directory. The
published artifacts are:

- `client-ca.pem`, the public certificate used to extend etcd's client trust;
- `../../cluster/etcdetcetc/client-ca.yaml`, encrypted to both the Fabric
  runtime age identity and Sam's recovery identity.

The CA has a critical client-auth EKU, a path length of zero, and a three-year
lifetime. Tenant leaves are fixed at 24 hours, renew eight hours before expiry,
rotate their private key on every issuance, and request only client auth.
Rotate the CA with an overlap window well before expiry; do not use the
generation helper as an in-place rotation mechanism.

Etcd must use a client trust bundle containing the original physical CA and
this public certificate for `--trusted-ca-file`. Its
`--peer-trusted-ca-file` remains the original physical CA alone. This preserves
all existing `fabric-root`, peer, observer, and recovery chains while ensuring
the online CA cannot mint a trusted etcd peer.

The installed consensus members are updated only through the guarded,
one-member-at-a-time helper:

```sh
../../../scripts/rollout-fabric-etcd-client-trust --check
```

Follow the complete rollout and rollback procedure in
[`../etcd/README.md`](../etcd/README.md). After every member accepts the client
bundle, use that runbook's `manage-etcdetcetc-admin` ceremony to create the
passwordless `fabric-etcdetcetc` user with exactly the built-in `root` role.
Client trust, the user binding, exact service-node network qualification, and
the suspended Flux activation are independent gates; passing one never implies
the others.
