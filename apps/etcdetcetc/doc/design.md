# etcdetcetc design

## Problem

Multi-tenant etcd clusters need per-tenant RBAC: users, roles, and
prefix-scoped permissions. Today this is done by hand with `etcdctl`, which is
error-prone, not declarative, and has no cleanup path. Decommissioning a tenant
means its keyspace rots in etcd forever.

## Goals

1. Declare etcd tenancy as Kubernetes resources (CRDs).
2. Automate user/role/permission lifecycle against a live etcd cluster.
3. Clean up completely on tenant deletion (purge keys, remove RBAC entities).
4. Emit a Secret per tenant with connection details for downstream consumers.
5. Keep the controller decoupled from cert-manager or any specific PKI -- it
   just reads and writes Secrets.

## Non-goals (v1alpha1)

- Managing etcd cluster deployment or lifecycle.
- Issuing or rotating TLS certificates (that's cert-manager's job).
- Multi-cluster scheduling or migration (future -- but the CRD shape
  accommodates it).

## CRDs

### EtcdCluster

Represents a live etcd cluster the controller can connect to.

```yaml
apiVersion: etcdetcetc.samcday.com/v1alpha1
kind: EtcdCluster
metadata:
  name: hub-etcd
  namespace: etcd-system
spec:
  endpoints:
    - https://hub-az1-cp1.hub.internal:2379
    - https://hub-az1-cp2.hub.internal:2379
    - https://hub-az1-cp3.hub.internal:2379
  authSecretRef:
    name: hub-etcd-root
status:
  connected: false
```

**spec.endpoints**: etcd client URLs.

**spec.authSecretRef**: reference to a Secret (same namespace) holding root
credentials. The controller detects the auth mode by inspecting which keys are
present:

| Mode  | Required keys                    |
|-------|----------------------------------|
| TLS   | `tls.crt`, `tls.key`, `ca.crt`  |
| Basic | `username`, `password`, `ca.crt` |

The `ca.crt` key is always required (TLS to etcd is non-negotiable).

**status.connected**: set to true when the controller has successfully
authenticated and pinged the cluster.

### EtcdTenant

Declares a tenant on an EtcdCluster. Namespace-scoped so it can live alongside
the workloads that consume it.

```yaml
apiVersion: etcdetcetc.samcday.com/v1alpha1
kind: EtcdTenant
metadata:
  name: cloud
  namespace: cloud-cluster
spec:
  clusterRef:
    name: hub-etcd
    namespace: etcd-system
  prefix: "/cloud/"
  secretName: cloud-etcd
status:
  ready: false
```

**spec.clusterRef**: cross-namespace reference to an EtcdCluster.

**spec.prefix**: etcd key prefix for this tenant. Defaults to `/<name>/` if
omitted.

**spec.secretName**: name of the output Secret to create in the tenant's
namespace. Defaults to `<name>-etcd` if omitted.

**status.ready**: set to true when user, role, and permissions are all
provisioned in etcd.

## Controller behaviour

### EtcdCluster reconciler

1. Read the auth Secret referenced by `spec.authSecretRef`.
2. Build an etcd client with the endpoints and credentials.
3. Ping the cluster (`etcdctl endpoint health` equivalent).
4. Update `status.connected`.
5. Cache the client for use by the EtcdTenant reconciler.

Watches: EtcdCluster, referenced Secrets (for credential rotation).

### EtcdTenant reconciler

**Create / Update:**

1. Resolve the referenced EtcdCluster. If not connected, requeue.
2. Compute effective prefix (`spec.prefix` or `/<name>/`).
3. Ensure etcd user exists with a generated password.
4. Ensure etcd role exists (named same as the user).
5. Ensure role has `readwrite` permission on the prefix.
6. Ensure role is granted to the user.
7. Create or update the output Secret (see below).
8. Set `status.ready = true`.

**Delete (finalizer: `etcdetcetc.samcday.com/tenant`):**

1. Delete all keys under the prefix.
2. Revoke role from user.
3. Delete user.
4. Delete role.
5. Remove finalizer. The output Secret is garbage-collected via ownerRef.

### Output Secret

Created in the same namespace as the EtcdTenant, owned by it.

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: <secretName>
  namespace: <tenant namespace>
  ownerReferences:
    - apiVersion: etcdetcetc.samcday.com/v1alpha1
      kind: EtcdTenant
      name: <tenant>
      controller: true
type: Opaque
data:
  username: <base64 tenant name>
  password: <base64 generated password>
```

### Output ConfigMap

Mirrored from the EtcdCluster's ConfigMap into the tenant's namespace, owned
by the EtcdTenant.

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: <tenant>-etcd
  namespace: <tenant namespace>
  ownerReferences:
    - apiVersion: etcdetcetc.samcday.com/v1alpha1
      kind: EtcdTenant
      name: <tenant>
      controller: true
data:
  endpoints: "https://host1:2379,https://host2:2379"
  ca.crt: |
    -----BEGIN CERTIFICATE-----
    ...
    -----END CERTIFICATE-----
```

Downstream consumers mount the Secret for credentials and the ConfigMap for
connection info. Client certs are handled separately by cert-manager -- this
controller doesn't touch PKI.

## Future roadmap

### Multiple EtcdCluster support

The architecture already supports this -- EtcdTenant references a specific
EtcdCluster. Future work:

- **Scheduling**: when `clusterRef` is omitted, a scheduler picks the best
  cluster based on capacity, locality, or policy. Pluggable via a trait /
  interface, similar to kube-scheduler's framework.
- **Capacity tracking**: EtcdCluster status reports tenant count, key count,
  and storage usage.

### Tenant migration

Move a tenant's keyspace from one EtcdCluster to another:

1. Snapshot keys under the prefix on the source cluster.
2. Restore them on the destination cluster with a new user/role.
3. Update the output Secret to point to the new cluster.
4. Purge the source keyspace.

This enables rebalancing, cluster decommissioning, and disaster recovery.
