# hub-etcd PKI

## Renew existing hub-cp leaf

The current leaf expires **2027-10-07 18:52 UTC**. Renewal is not automatic:
decommission Hub or renew the leaf before that deadline.

Renew the existing client identity, not its key or the CA:

1. Retain `hub-cp.csr.enc`, `hub-cp-key.pem.enc`, and the existing CA files.
   Check that the retained CSR and private key have the same public key.
2. Use `cfssl sign` (not `cfssl gencert`) with the decrypted existing CSR,
   existing CA certificate/key, and `-config client-config.json`. Its default
   client profile grants `client auth` with a one-year (`8760h`) expiry.
   Do not regenerate the CSR or private key.
3. Validate the returned leaf: its public key must match the retained CSR/key,
   subject CN must be `hub-cp`, EKU must include `clientAuth`, validity must cover
   the intended renewal period, and its signature must verify under the existing
   CA. Read the actual validity dates from the issued certificate.
4. Keep decrypted private inputs in memory or pipes (Bash process substitutions
   are suitable); never write private plaintext to disk.
   Do not use `cfssljson -bare` for this renewal. Encrypt only the validated leaf
   into `hub-cp.pem.enc`, retaining the existing SOPS recipients/rules. The CA,
   CSR, private key, and peer/admin credentials stay unchanged.

Git alone does not deploy the leaf to existing hosts. A separately approved live
operation must atomically replace only
`/var/lib/rancher/k3s/server/etcd-client.pem`, retaining its ownership, permissions,
SELinux label, and the existing CA/key, in order **cp2 -> cp3 -> cp1**. After each
host, use context `hub` and that node's direct API endpoint to require both etcd
readiness checks, full server readiness, and a successful actual CDI GET before
proceeding. Allow up to five minutes for client reconnect backoff; stop if either
readiness or the resource read still fails. No automatic service restarts or etcd
certificate, authentication, or configuration changes are part of this renewal.

Hub is being retired, so the required K3s datastore CA/client-certificate/key
copies are intentionally omitted from `hub/butane/control-plane.yaml`. Node
reprovisioning would require restoring them; the CA/peer/admin credentials in
`hub/butane/etcd.yaml` are unaffected.

## Bootstrap (historical; deprecated for provisioning)

These original commands generate new keys and materialize plaintext files; they
are historical context, not the renewal workflow above. Further Butane-based Hub
node reprovisioning is not expected during retirement and would require restoring
the omitted datastore credentials before any separately approved provisioning.

```
# generate root self-signed CA cert
cfssl gencert -initca ca-csr.json | cfssljson -bare ca
sops --encrypt ca.csr > ca.csr.enc
sops --encrypt ca-key.pem > ca-key.pem.enc
sops --encrypt ca.pem > ca.pem.enc
rm *.pem *.csr

# generate peer/server cert
cfssl gencert -ca <(sops --decrypt ca.pem.enc) -ca-key <(sops --decrypt ca-key.pem.enc) -config peer-config.json peer-csr.json | cfssljson -bare peer
sops --encrypt peer.csr > peer.csr.enc
sops --encrypt peer-key.pem > peer-key.pem.enc
sops --encrypt peer.pem > peer.pem.enc
rm *.pem *.csr

# generate root user (RBAC) client cert
cfssl gencert -ca <(sops --decrypt ca.pem.enc) -ca-key <(sops --decrypt ca-key.pem.enc) -config client-config.json root-csr.json | cfssljson -bare root
sops --encrypt root.csr > root.csr.enc
sops --encrypt root-key.pem > root-key.pem.enc
sops --encrypt root.pem > root.pem.enc
rm *.pem *.csr

# generate hub-cp client cert
cfssl gencert -ca <(sops --decrypt ca.pem.enc) -ca-key <(sops --decrypt ca-key.pem.enc) -config client-config.json hub-cp-csr.json | cfssljson -bare hub-cp
sops --encrypt hub-cp.csr > hub-cp.csr.enc
sops --encrypt hub-cp-key.pem > hub-cp-key.pem.enc
sops --encrypt hub-cp.pem > hub-cp.pem.enc
rm *.pem *.csr
```
