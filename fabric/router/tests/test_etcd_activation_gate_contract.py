#!/usr/bin/env python3

from __future__ import annotations

import pathlib
import re
import unittest

import yaml


REPO = pathlib.Path(__file__).resolve().parents[3]
PRE_OPEN = REPO / "scripts" / "verify-fabric-etcd-pre-open"
ROUTER_POLICY = REPO / "scripts" / "rollout-fabric-router-etcd-policy"
IMAGE_WORKFLOW = REPO / ".github" / "workflows" / "images.yaml"


def bash_function(source: str, name: str) -> str:
    function = re.search(
        rf"(?ms)^{re.escape(name)}\(\) \{{\n(?P<body>.*?)^\}}$",
        source,
    )
    if function is None:
        raise AssertionError(f"cannot find Bash function {name}")
    return function.group("body")


class EtcdActivationGateContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.pre_open = PRE_OPEN.read_text(encoding="utf-8")
        cls.router_policy = ROUTER_POLICY.read_text(encoding="utf-8")
        cls.hub_pre_open = bash_function(cls.pre_open, "verify_hub_reconciliation")
        cls.pre_open_absence = bash_function(
            cls.pre_open, "verify_no_controller_or_runtime"
        )
        cls.image_verifier = bash_function(cls.pre_open, "verify_image_pin")
        cls.activation_fence = bash_function(
            cls.router_policy, "verify_activation_fence"
        )
        cls.image_workflow = yaml.safe_load(
            IMAGE_WORKFLOW.read_text(encoding="utf-8")
        )

    def assert_hub_reconciliation_contract(self, source: str) -> None:
        for required in (
            "get gitrepository infra",
            '.spec.ref == {branch: "main"}',
            ".status.artifact.revision == $revision",
            "infra etcdetcetc cloud-cluster --output=json",
            'if $name == "infra" then "hub/cluster/flux-system"',
            'elif $name == "etcdetcetc" then "hub/cluster/etcdetcetc"',
            'elif $name == "cloud-cluster" then "hub/cluster/cloud-cluster"',
            'kind: "GitRepository", name: "infra", namespace: "flux-system"',
            ".status.lastAppliedRevision == $revision",
            "get helmrelease kustomizations",
            '.spec.chart.spec.chart == "charts/resources"',
            '.spec.chart.spec.reconcileStrategy == "Revision"',
            ".status.lastAttemptedRevision == $revision",
            'if .type == "Reconciling" or .type == "Stalled" then .status != "True"',
            "helmcharts.source.toolkit.fluxcd.io flux-system-kustomizations",
            '.spec.chart == "charts/resources"',
            '.spec.reconcileStrategy == "Revision"',
            '.spec.sourceRef == {kind: "GitRepository", name: "infra"}',
        ):
            with self.subTest(required=required):
                self.assertIn(required, source)

        # Every reconciler in the chain must be live, current-generation, and
        # Ready; checking only a revision string can accept a pending object.
        self.assertGreaterEqual(
            source.count(".status.observedGeneration == $generation"), 4
        )
        self.assertGreaterEqual(
            source.count('.type == "Ready" and .status == "True"'), 4
        )
        self.assertGreaterEqual(
            source.count(".observedGeneration == $generation"), 8
        )

    def test_pre_open_proves_the_exact_hub_reconciliation_chain(self) -> None:
        self.assertIn(
            'expected_chart_revision="0.0.0+${head:0:12}"', self.pre_open
        )
        self.assert_hub_reconciliation_contract(self.hub_pre_open)
        self.assertEqual(self.pre_open.count("verify_hub_reconciliation\n"), 1)
        for command_failure in (
            "cannot read the Hub infra GitRepository",
            "cannot read the Hub owner Kustomizations for the legacy releases",
            "cannot read the Hub Kustomization fan-out HelmRelease",
            "cannot read the Hub Kustomization fan-out HelmChart",
        ):
            with self.subTest(command_failure=command_failure):
                self.assertRegex(
                    self.hub_pre_open,
                    re.compile(
                        rf"\|\|\s+die ['\"]{re.escape(command_failure)}['\"]"
                    ),
                )

    def test_activation_fence_resamples_the_same_hub_chain_before_and_after(self) -> None:
        self.assertIn("for suffix in before after; do", self.activation_fence)
        self.assert_hub_reconciliation_contract(self.activation_fence)
        self.assertIn('--arg revision "main@sha1:$head"', self.activation_fence)
        self.assertEqual(
            self.activation_fence.count('--arg revision "0.0.0+${head:0:12}"'),
            2,
        )
        for command_failure in (
            "cannot read the Hub source at the $stage activation fence",
            "cannot read Hub owner Kustomizations at the $stage activation fence",
            "cannot read the Hub fan-out HelmRelease at the $stage activation fence",
            "cannot read the Hub fan-out HelmChart at the $stage activation fence",
        ):
            with self.subTest(command_failure=command_failure):
                self.assertIn(f'||\n      die "{command_failure}"', self.activation_fence)

    def assert_smoke_contract(self, source: str, *, activation: bool) -> None:
        for required in (
            "--namespace etcdetcetc-smoke get",
            "deployments,replicasets,statefulsets,daemonsets,jobs,cronjobs,replicationcontrollers,pods",
            ".items == []",
            "get namespace etcdetcetc-smoke --output=json",
            '.metadata.name == "etcdetcetc-smoke"',
            ".metadata.deletionTimestamp == null",
            "--namespace etcdetcetc-smoke get networkpolicy",
            "(.items | length) == 1",
            '.items[0].metadata.name == "default-deny"',
            '.items[0].metadata.namespace == "etcdetcetc-smoke"',
            ".items[0].spec == {",
            'podSelector: {}, policyTypes: ["Egress", "Ingress"]',
            "secret/fabric-smoke-etcd",
            "configmap/fabric-smoke-etcd",
            "--ignore-not-found --output=name",
            "[[ -z $object_name ]] ||",
        ):
            with self.subTest(required=required, activation=activation):
                self.assertIn(required, source)

        # The only permitted policy is the permanent default-deny, so the
        # temporary post-open policy is necessarily absent.
        self.assertNotIn("fabric-etcd-post-open", source)
        self.assertNotIn("|| true", source)

        if activation:
            failures = (
                "cannot inventory smoke workloads at the $stage/$suffix activation fence",
                "cannot read the smoke Namespace at the $stage/$suffix activation fence",
                "cannot inventory smoke NetworkPolicies at the $stage/$suffix activation fence",
                "cannot prove smoke artifact absent at the $stage/$suffix fence: $resource",
            )
        else:
            failures = (
                "cannot inventory pre-open smoke workloads",
                "cannot read the permanent smoke Namespace",
                "cannot inventory pre-open smoke NetworkPolicies",
                "cannot prove pre-open smoke artifact absent: $resource",
            )
        for failure in failures:
            with self.subTest(failure=failure, activation=activation):
                self.assertIn(failure, source)

    def test_pre_open_smoke_absence_contract_is_exact_and_fail_closed(self) -> None:
        self.assert_smoke_contract(self.pre_open_absence, activation=False)
        for command in (
            r'inventory=\$\("\$\{ik\[@\]\}" --namespace etcdetcetc-smoke get '
            r'.*?--output=json\) \|\| die',
            r'namespace=\$\("\$\{ik\[@\]\}" get namespace etcdetcetc-smoke '
            r'--output=json\) \|\|\n    die',
            r'network_policies=\$\("\$\{ik\[@\]\}" --namespace '
            r'etcdetcetc-smoke get networkpolicy .*?--output=json\) \|\| die',
            r'object_name=\$\("\$\{ik\[@\]\}" --namespace etcdetcetc-smoke '
            r'get "\$resource" .*?--ignore-not-found --output=name\) \|\|',
        ):
            with self.subTest(command=command):
                self.assertRegex(self.pre_open_absence, re.compile(command, re.DOTALL))

    def test_activation_fence_smoke_absence_is_resampled_and_fail_closed(self) -> None:
        self.assert_smoke_contract(self.activation_fence, activation=True)
        self.assertIn("for suffix in before after; do", self.activation_fence)
        for command in (
            r'operator_kubectl --namespace etcdetcetc-smoke get .*?'
            r'--output=json >"\$smoke_inventory" \|\|\n      die',
            r'operator_kubectl get namespace etcdetcetc-smoke --output=json '
            r'\\\n      >"\$smoke_namespace" \|\|\n      die',
            r'operator_kubectl --namespace etcdetcetc-smoke get networkpolicy '
            r'--output=json \\\n      >"\$smoke_network_policies" \|\|\n      die',
            r'object_name=\$\(operator_kubectl --namespace etcdetcetc-smoke '
            r'get "\$resource" .*?--ignore-not-found --output=name\) \|\|',
        ):
            with self.subTest(command=command):
                self.assertRegex(self.activation_fence, re.compile(command, re.DOTALL))

    def test_image_workflow_emits_registry_bound_github_provenance(self) -> None:
        build = self.image_workflow["jobs"]["build"]
        self.assertIs(build["strategy"]["fail-fast"], False)
        self.assertEqual(
            build["permissions"],
            {
                "attestations": "write",
                "contents": "read",
                "id-token": "write",
                "packages": "write",
            },
        )
        scope_step = next(step for step in build["steps"] if step.get("id") == "scope")
        self.assertIn('"apps/${{ matrix.name }}"', scope_step["run"])
        self.assertIn('"${{ github.event.before }}"', scope_step["run"])
        self.assertIn('"workflow_dispatch"', scope_step["run"])
        tag_step = next(
            step for step in build["steps"] if step.get("id") == "image-tag"
        )
        self.assertEqual(tag_step["if"], "steps.scope.outputs.changed == 'true'")
        self.assertIn("printf -v run_id '%020d'", tag_step["run"])
        metadata_step = next(
            step for step in build["steps"] if step.get("id") == "meta"
        )
        self.assertEqual(
            metadata_step["with"]["tags"].strip(),
            "type=raw,value=${{ steps.image-tag.outputs.value }}",
        )
        attest_steps = [
            step
            for step in build["steps"]
            if step.get("uses", "").startswith("actions/attest@")
        ]
        self.assertEqual(
            attest_steps,
            [
                {
                    "name": "Attest pushed image provenance",
                    "if": "steps.scope.outputs.changed == 'true' && github.event_name != 'pull_request'",
                    "uses": "actions/attest@f7c74d28b9d84cb8768d0b8ca14a4bac6ef463e6",
                    "with": {
                        "subject-name": "${{ env.REGISTRY }}/${{ github.repository }}-${{ matrix.name }}",
                        "subject-digest": "${{ steps.build-and-push.outputs.digest }}",
                        "push-to-registry": True,
                        "create-storage-record": False,
                    },
                }
            ],
        )

    def test_image_verifier_binds_attestation_and_exact_workflow_blob(self) -> None:
        workflow_blob = self.image_verifier.index(
            'image_workflow=$(git -C "$repo_root" rev-parse'
        )
        head_blob = self.image_verifier.index(
            'head_workflow=$(git -C "$repo_root" rev-parse', workflow_blob
        )
        equality = self.image_verifier.index(
            '[[ $image_workflow == "$head_workflow" ]]', head_blob
        )
        attestation = self.image_verifier.index(
            'provenance=$(gh attestation verify "oci://$repository@$digest"', equality
        )
        self.assertEqual(
            [workflow_blob, head_blob, equality, attestation],
            sorted([workflow_blob, head_blob, equality, attestation]),
        )
        for required in (
            '"$image_revision:.github/workflows/images.yaml"',
            '"$head:.github/workflows/images.yaml"',
            "attested controller image used a different build workflow than the reviewed revision",
            'gh attestation verify "oci://$repository@$digest"',
            "--repo samcday/infra",
            "--signer-workflow samcday/infra/.github/workflows/images.yaml",
            '--source-digest "$image_revision"',
            "--source-ref refs/heads/main",
            "--deny-self-hosted-runners",
            "--format=json",
            "pinned controller image lacks trusted GitHub Actions build provenance",
            '"https://slsa.dev/provenance/v1"',
            '.name == $name and .digest == {sha256: $digest}',
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.image_verifier)


if __name__ == "__main__":
    unittest.main(verbosity=2)
