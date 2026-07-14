# fabric recovery identities

The fabric has its own SOPS age identity. Runtime secrets are encrypted to
both that identity and Sam's offline personal identity. The private fabric
identity itself is encrypted only to Sam's personal identity in
`sops-age-key.txt.enc`; this avoids a circular disaster-recovery dependency.
Offline consensus PKI and Butane bootstrap profiles are also personal-only;
they are deliberately outside the live fabric identity's trust boundary.

Never decrypt private material into this directory. Bootstrap scripts use a
mode-0700 temporary directory and remove it on exit.

`flux-deploy-key.pub` is the reserved public half of the future read-only
GitHub deploy key. It is deliberately not embedded in the phase-one consensus
Ignitions. Add it to `samcday/infra` without write access only when Flux is
ready to be placed on workers. Its expected SHA256 fingerprint is
`ZVbsCrsuAzDJv+dcthCVSyEW1USRSxy/nkKTnxC33dU`.
