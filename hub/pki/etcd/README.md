# hub-etcd PKI

## Bootstrap

```
# generate root self-signed CA cert
cfssl gencert -initca ca-csr.json | cfssljson -bare ca
sops --encrypt ca.csr > ca.csr.enc
sops --encrypt ca-key.pem > ca-key.pem.enc

# generate peer/server cert
cfssl gencert -ca ca.pem -ca-key <(sops --decrypt ca-key.pem.enc) -config peer-config.json peer-csr.json | cfssljson -bare peer
sops --encrypt peer.csr > peer.csr.enc
sops --encrypt peer-key.pem > peer-key.pem.enc

# generate root user (RBAC) client cert
cfssl gencert -ca ca.pem -ca-key <(sops --decrypt ca-key.pem.enc) -config client-config.json root-csr.json | cfssljson -bare root
sops --encrypt root.csr > root.csr.enc
sops --encrypt root-key.pem > root-key.pem.enc

# generate hub-cp client cert
cfssl gencert -ca ca.pem -ca-key <(sops --decrypt ca-key.pem.enc) -config client-config.json hub-cp-csr.json | cfssljson -bare hub-cp
sops --encrypt hub-cp.csr > hub-cp.csr.enc
sops --encrypt hub-cp-key.pem > hub-cp-key.pem.enc

```
