# hub-etcd PKI

## Bootstrap

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
