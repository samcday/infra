"""Focused render gates for the additive LiteLLM pilot (no live cluster writes)."""
import os
from pathlib import Path
import subprocess
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[2]
CHART_VERSION = '1.105.1'
IMAGE = 'ghcr.io/berriai/litellm-non_root:v1.105.1@sha256:aff05d5e4bc3824e079e8461e85d64feb6c833612af2fe1717a1aa8483b46da4'
PG_IMAGE = 'docker.io/library/postgres:17-alpine@sha256:aa90e97ee862e558111d34cfb8b2c4bec768c2b039fb791341686928560263b3'


def documents(text):
    return [x for x in yaml.safe_load_all(text) if x]


def resource(items, kind, name):
    matches = [x for x in items if x['kind'] == kind and x['metadata']['name'] == name]
    assert len(matches) == 1, (kind, name)
    return matches[0]


def manifests():
    assert (ROOT / 'kustomization.yaml').exists(), 'Missing additive LiteLLM manifests'
    return documents(subprocess.run(['kubectl', 'kustomize', str(ROOT)], check=True, capture_output=True, text=True).stdout)


def values():
    return resource(manifests(), 'HelmRelease', 'litellm')['spec']['values']


class Pilot(unittest.TestCase):
    def test_existing_gateway_untouched(self):
        subprocess.run(['git', '-C', str(REPO), 'diff', '--exit-code', 'origin/main', '--', 'hub/cluster/llm-gateway'], check=True, capture_output=True)

    def test_pinned_single_process_and_persistent_database(self):
        objects = manifests()
        hr = resource(objects, 'HelmRelease', 'litellm')['spec']
        self.assertEqual(hr['chart']['spec']['version'], CHART_VERSION)
        v = hr['values']
        self.assertEqual(v['image']['repository'] + ':' + v['image']['tag'], IMAGE)
        self.assertEqual((v['replicaCount'], v['numWorkers'], v['strategy']['type']), (1, 1, 'Recreate'))
        self.assertFalse(v['db']['deployStandalone'])  # no archived Bitnami database image
        self.assertTrue(v['db']['useExisting'])
        pg = resource(objects, 'StatefulSet', 'litellm-postgres')['spec']
        self.assertEqual(pg['template']['spec']['containers'][0]['image'], PG_IMAGE)
        self.assertEqual(pg['volumeClaimTemplates'][0]['spec']['storageClassName'], 'ceph-block')

    def test_only_inference_public(self):
        route = resource(manifests(), 'HTTPRoute', 'litellm')['spec']
        self.assertEqual(route['hostnames'], ['litellm.samcday.com'])
        paths = set()
        for rule in route['rules']:
            self.assertEqual(rule['backendRefs'], [{'name': 'litellm', 'port': 4000}])
            for match in rule['matches']:
                self.assertEqual(match['path']['type'], 'Exact')
                self.assertTrue(any(h['name'] == 'CF-Visitor' and 'https' in h['value'] for h in match['headers']))
                paths.add((match['method'], match['path']['value']))
        self.assertEqual(paths, {('GET', '/v1/models'), ('POST', '/v1/responses'),
                                ('POST', '/v1/chat/completions'), ('POST', '/v1/messages')})

    def test_secret_references_and_no_fallback(self):
        v = values()
        self.assertFalse(v['serviceAccount']['automount'])
        self.assertTrue(v['securityContext']['runAsNonRoot'])
        self.assertEqual(v['securityContext']['capabilities']['drop'], ['ALL'])
        self.assertFalse(v['securityContext']['allowPrivilegeEscalation'])
        config = v['proxy_config']
        self.assertEqual({m['model_name'] for m in config['model_list']}, {'gpt-6-astra', 'claude-haiku-5-5'})
        for model in config['model_list']:
            self.assertTrue(model['litellm_params']['api_base'].startswith('http://cliproxyapi.llm-gateway.svc.cluster.local:8317'))
            self.assertEqual(model['litellm_params']['api_key'], 'os.environ/CLIPROXY_API_KEY')
        self.assertEqual(config['general_settings']['master_key'], 'os.environ/PROXY_MASTER_KEY')
        self.assertFalse(config['litellm_settings']['telemetry'])
        self.assertTrue(config['litellm_settings']['turn_off_message_logging'])
        self.assertFalse(config['general_settings']['store_prompts_in_spend_logs'])
        self.assertFalse(config.get('router_settings', {}).get('fallbacks'))
        secret = resource(manifests(), 'Secret', 'litellm-credentials')
        self.assertIn('sops', secret)
        self.assertEqual(set(secret['stringData']), {'masterkey', 'salt', 'username', 'password', 'CLIPROXY_API_KEY'})
        self.assertTrue(all(x.startswith('ENC[AES256_GCM,') for x in secret['stringData'].values()))

    def test_bot_can_manage_only_this_application(self):
        objects = manifests()
        role = resource(objects, 'Role', 'litellm-operator')
        secret_rules = [r for r in role['rules'] if 'secrets' in r['resources']]
        self.assertEqual(secret_rules, [{'apiGroups': [''], 'resources': ['secrets'],
                                         'resourceNames': ['litellm-credentials'], 'verbs': ['get']}])
        self.assertFalse(any('*' in r['resources'] or '*' in r['verbs'] for r in role['rules']))
        self.assertFalse(any('helmreleases' in r['resources'] and set(r['verbs']) & {'patch', 'update'} for r in role['rules']))
        self.assertFalse(any(x['kind'] == 'ClusterRoleBinding' for x in objects))
        self.assertEqual(resource(objects, 'RoleBinding', 'litellm-operator')['subjects'],
                         [{'kind': 'ServiceAccount', 'name': 'litellm-operator', 'namespace': 'litellm'}])

    def test_real_chart_renders_private_nonroot_workload(self):
        v = values()
        chart = os.environ.get('LITELLM_CHART_DIR', 'oci://ghcr.io/berriai/litellm-helm')
        rendered = subprocess.run(['helm', 'template', 'litellm', chart, '--namespace', 'litellm',
                                   '--version', CHART_VERSION, '-f', '-'], input=yaml.safe_dump(v),
                                  capture_output=True, text=True, check=True, timeout=120)
        objects = documents(rendered.stdout)
        deployment = resource(objects, 'Deployment', 'litellm')['spec']['template']['spec']
        self.assertEqual(deployment['containers'][0]['image'], IMAGE)
        self.assertFalse(resource(manifests(), 'ServiceAccount', 'litellm')['automountServiceAccountToken'])
        self.assertEqual(resource(objects, 'Service', 'litellm')['spec']['type'], 'ClusterIP')
        migration = resource(objects, 'Job', 'litellm-migrations')
        self.assertEqual(migration['metadata']['annotations']['helm.sh/hook'], 'pre-install,pre-upgrade')
        self.assertFalse(any(x['kind'] == 'Ingress' for x in objects))
        self.assertIn('checksum/config', resource(objects, 'Deployment', 'litellm')['spec']['template']['metadata']['annotations'])


if __name__ == '__main__':
    unittest.main()
