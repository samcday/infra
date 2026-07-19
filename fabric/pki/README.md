# fabric recovery identities

The fabric has its own SOPS age identity. Runtime secrets are encrypted to
both that identity and Sam's offline personal identity. The private fabric
identity itself is encrypted only to Sam's personal identity in
`sops-age-key.txt.enc`; this avoids a circular disaster-recovery dependency.
Offline consensus PKI and Butane bootstrap profiles are also personal-only;
they are deliberately outside the live fabric identity's trust boundary.

Never decrypt private material into this directory. Bootstrap scripts use a
mode-0700 temporary directory and remove it on exit.

`flux-deploy-key.pub` is retained as unused recovery history. The live Flux
design reads the public `samcday/infra` repository over HTTPS and therefore
does not install a long-lived GitHub credential. Do not add the reserved key to
GitHub unless the repository later becomes private and the trust boundary is
reviewed again. Its expected SHA256 fingerprint is
`ZVbsCrsuAzDJv+dcthCVSyEW1USRSxy/nkKTnxC33dU`.
