# Stable Fabric SSH host identities

Each of the five inventory nodes has one ED25519 host key generated for its
stable node name. The private key is SOPS-encrypted only to Sam's offline age
identity; the public key is committed beside it and pinned in
`fabric/access/known_hosts`.

The root and service-node Butane renderers decrypt only the selected node's
key on verified tmpfs, prove the private/public pair, and merge it into that
node's Ignition. A Bootie reprovision therefore retains the same SSH trust
decision instead of requiring a console fingerprint ceremony after every
disk replacement. Never share a key between node names and never use these
host keys as client identities.
