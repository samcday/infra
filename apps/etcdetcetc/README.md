# etcdetcetc

**etcd external tenant controller, etc.**

A Kubernetes controller that manages multi-tenant etcd access. Automates the
lifecycle of etcd users, roles, and prefix-scoped permissions -- the tedious
access-control plumbing that makes shared etcd clusters actually work.

## CRDs

### EtcdCluster

Declares an etcd cluster the controller can manage tenants on.

```yaml
apiVersion: etcdetcetc.samcday.com/v1alpha1
kind: EtcdCluster
metadata:
  name: hub-etcd
spec:
  endpoints:
    - https://etcd1.example.com:2379
    - https://etcd2.example.com:2379
    - https://etcd3.example.com:2379
  authSecretRef:
    name: etcd-root-credentials
```

The referenced Secret holds root/admin credentials. Supports:
- **TLS cert auth**: keys `tls.crt`, `tls.key`, `ca.crt`
- **Basic auth**: keys `username`, `password`, `ca.crt`

### EtcdTenant

Carves out a keyspace on an EtcdCluster for a tenant.

```yaml
apiVersion: etcdetcetc.samcday.com/v1alpha1
kind: EtcdTenant
metadata:
  name: my-app
  namespace: my-app
spec:
  clusterRef:
    name: hub-etcd
    namespace: etcd-system
```

The controller creates the etcd user, role, and prefix permissions, then emits
a Secret with credentials and a ConfigMap with connection info. On deletion, the keyspace is purged and access-control
entities are removed.

Defaults:
- `prefix`: `/<name>/`
- `secretName`: `<name>-etcd`

## Future

- Multiple EtcdCluster support with pluggable tenant scheduling
- Tenant migration between clusters
