# Private services over the tailnet

The Headscale `services-subnet-router` StatefulSet advertises **only
`10.0.2.0/24`**. This covers the hub service addresses (Cilium's current BGP
allocation pool is `10.0.2.1-10.0.2.127`). It does not reinstate the removed hub
LAN (`10.0.1.0/24`), Pod (`172.30.0.0/16`), or ClusterIP (`172.31.0.0/16`)
advertisements.

## Design and boundaries

- Two non-root, userspace Tailscale routers run on distinct hub workers. They do
  not use host networking, TUN, forwarding sysctls, or network-administration
  capabilities, and explicitly reject other routers' subnet routes.
- Enrollment uses a dedicated `tag:hub-services-subnet-router` pre-auth key from
  the existing Headscale OpenTofu controller. Only that tag auto-approves the
  services prefix. The key follows the existing long-lived infrastructure key
  pattern; treat the generated authentication Secret as a credential.
- Each ordinal persists its own node identity in its named Kubernetes Secret.
  Fresh names avoid restoring stale settings from the old `subnet-router-*`
  identities. Keep two replicas in sync with the two precreated state Secrets
  and the restricted Role; do not scale blindly.
- A disruption budget preserves one replica during voluntary evictions.
  Readiness checks Tailscale's local health endpoint; it does not prove the
  service-network path. Headscale/Tailscale choose and fail over subnet routers,
  not the headless Kubernetes Service. A lost backend/BGP path still needs
  separate diagnosis.
- Userspace subnet forwarding supports TCP/UDP, not arbitrary IP protocols, and
  service backends see the router-side connection rather than the client's
  tailnet source IP. Do not use ICMP alone as the acceptance test.
- Existing ACL access is unchanged: `sam@` already has `*:*`. This is private
  network access, **not** a security boundary against agents running on an
  already-authorized device. Tagged infrastructure gains no new traffic grants.
- The subnet router does not change service DNS, TLS, Gateway/HTTPRoute, or
  Cloudflare configuration.

## Verify after an approved GitOps rollout

Do not apply these manifests or change live routes merely to run local tests.
Merging into the watched branch allows the existing Flux/tofu-controller flow
(with automatic plan approval) to deploy them.

1. Check `headscale` Flux reconciliation and `Terraform/tofu` in namespace `headscale`. The
   controller must create `services-subnet-router-ts-auth`; pods may wait for
   this Secret during first reconciliation.
2. Check `StatefulSet/services-subnet-router` has two ready replicas on distinct
   worker nodes. Check startup logs without dumping auth/state Secrets.
3. In Headscale, inspect both nodes and their advertised/approved routes. Both
   must advertise `10.0.2.0/24` and no other prefix; one is normally primary.
   Inspect the `headscale nodes list-routes` output using the deployed release.
4. On a Linux client, verify it accepts subnet routes (`tailscale debug prefs`,
   `RouteAll: true`). Inspect `tailscale status --json` and `ip -4 route get
   10.0.2.4`; the selected route should use Tailscale, not the local Wi-Fi
   gateway. Do not blanket-enable route acceptance on infrastructure nodes.
5. From outside the hub LAN, test the actual TLS endpoint:

   ```sh
   curl --noproxy '*' --fail --show-error --connect-timeout 5 --max-time 15 \
     https://grafana.hub.samcday.com/api/health
   ```

   Expect HTTP success with normal certificate verification. Confirm DNS still
   resolves to the private service address, and that no removed prefix appears
   among the clients' received routes.
6. Failover testing requires separate approval for a controlled disruption;
   readiness/replica count alone is not proof of end-to-end failover.

## Rollback

Revert the restoration commit through GitOps. This removes the StatefulSet,
its supporting resources, the dedicated key/Secret, and its auto-approval entry;
existing private DNS/TLS configuration remains unchanged. Offline node records
may remain in Headscale: removing auto-approval alone does not revoke an already
approved route. Inspect retained records/routes and obtain explicit approval
before any manual Headscale cleanup. Reverting does not recover connectivity;
it returns off-LAN clients to the previous lack of a services route.
