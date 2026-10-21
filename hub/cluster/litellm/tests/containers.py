"""Actual LiteLLM/Postgres, isolated mock upstream, fake credentials, no host ports.

Requires the pinned images already pulled. Never reads SOPS plaintext or a cluster.
"""
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import tempfile
import time
from urllib.parse import quote
from typing import Any

import yaml
from static import IMAGE, PG_IMAGE, ROOT, values

FIXTURES = ROOT.parent / 'llm-gateway/tests'
MOCK_IMAGE = 'python:3.13-alpine'
CHECKS = []
PRIVATE = []


def check(name, condition):
    assert condition, name
    CHECKS.append(name)
    print('PASS ' + name, flush=True)


class Lab:
    def __init__(self):
        self.name = 'litellm-test-' + secrets.token_hex(5)
        self.containers = []
        self.mock = ''
        self.env = dict(os.environ)
        self.env.update(PROXY_MASTER_KEY='sk-' + secrets.token_hex(24),
                        CLIPROXY_API_KEY=secrets.token_hex(24),
                        LITELLM_SALT_KEY=secrets.token_hex(24),
                        POSTGRES_PASSWORD=secrets.token_hex(24),
                        POSTGRES_USER='litellm', POSTGRES_DB='litellm',
                        PGDATA='/var/lib/postgresql/data/pgdata')
        self.env['DATABASE_URL'] = 'postgresql://litellm:' + self.env['POSTGRES_PASSWORD'] + '@postgres:5432/litellm'
        PRIVATE.extend(self.env[k] for k in ('PROXY_MASTER_KEY', 'CLIPROXY_API_KEY', 'LITELLM_SALT_KEY', 'POSTGRES_PASSWORD'))
        self.env.update(values()['envVars'])
        self.volume = self.name + '-pgdata'

    def docker(self, *args, stdin=None, required=True, timeout=90):
        r = subprocess.run(['docker', *args], input=stdin, env=self.env,
                           capture_output=True, text=True, timeout=timeout)
        if required and r.returncode:
            raise RuntimeError('docker ' + args[0] + ' failed: ' + r.stderr[-1000:])
        return (r.stdout + (r.stderr if args[0] == 'logs' else '')).strip()

    def start(self, suffix, image, args=(), env=(), extra=(), entrypoint=None):
        name = self.name + '-' + suffix
        self.containers.append(name)
        command = ['run', '-d', '--pull=never', '--name', name, '--network', self.name,
                   '--network-alias', suffix, '--cap-drop=ALL', '--security-opt=no-new-privileges',
                   '--security-opt=label=disable', *extra]
        for key in env:
            command += ['-e', key]
        if entrypoint:
            command += ['--entrypoint', entrypoint]
        self.docker(*command, image, *args)
        ports = self.docker('inspect', '--format', '{{json .HostConfig.PortBindings}}', name)
        assert ports in ('null', '{}'), 'test must not publish ports'
        return name

    def request(self, path, body=None, key=None, base='http://proxy:4000', expected: int | None = 200) -> Any:
        headers = {'Authorization': 'Bearer ' + key} if key else {}
        raw = self.docker('exec', '-i', self.mock, 'python3', '-B', '/test/http_client.py',
                          stdin=json.dumps({'url': base + path, 'body': body, 'headers': headers}))
        status, data = json.loads(raw)
        if expected is not None:
            assert status == expected, f'{path}: expected {expected}, got {status}'
        return data if expected is not None else (status, data)

    def ready(self):
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if self.request('/health/readiness', key=self.env['PROXY_MASTER_KEY'], expected=None)[0] == 200:
                return
            time.sleep(1)
        raise AssertionError('LiteLLM did not become ready')

    def key(self, alias, **overrides):
        body = dict(key_alias=alias, key_type='llm_api', models=['gpt-6-astra'],
                    duration='7d', max_budget=1.0, budget_duration=None, rpm_limit=60,
                    max_parallel_requests=1)
        body.update(overrides)
        result = self.request('/key/generate', body, self.env['PROXY_MASTER_KEY'])
        PRIVATE.append(result['key'])
        return result['key']

    def info(self, key):
        return self.request('/key/info?key=' + quote(key), key=self.env['PROXY_MASTER_KEY'])['info']

    def usage(self, key, minimum):
        deadline = time.monotonic() + 95
        while time.monotonic() < deadline:
            info = self.info(key)
            if info['spend'] >= minimum - 1e-10:
                return info
            time.sleep(1)
        raise AssertionError('usage not persisted within 95 seconds')

    def cleanup(self):
        for name in reversed(self.containers):
            self.docker('rm', '-f', name, required=False)
        self.docker('volume', 'rm', self.volume, required=False)
        self.docker('network', 'rm', self.name, required=False)
        remaining = self.docker('ps', '-a', '--filter', 'name=' + self.name, '--format', '{{.Names}}')
        assert not remaining, 'test container cleanup incomplete'


def run(lab, root):
    for image in (IMAGE, PG_IMAGE, MOCK_IMAGE):
        lab.docker('image', 'inspect', image)
    lab.docker('network', 'create', '--internal', lab.name)
    assert lab.docker('network', 'inspect', '--format', '{{.Internal}}', lab.name) == 'true'
    lab.docker('volume', 'create', lab.volume)
    lab.docker('run', '--rm', '--pull=never', '--network', 'none', '-v', lab.volume + ':/data',
               '--entrypoint', 'chown', PG_IMAGE, '70:70', '/data')
    lab.pg = lab.start('postgres', PG_IMAGE, env=('POSTGRES_USER', 'POSTGRES_PASSWORD', 'POSTGRES_DB', 'PGDATA'),
                       extra=('--user', '70:70', '-v', lab.volume + ':/var/lib/postgresql/data'))
    # Extend the shared fixture locally: cached/reasoning tokens are subsets,
    # not extra tokens to charge twice. Do not modify the existing gateway tests.
    fixture = (FIXTURES / 'mock_provider.py').read_text()
    usage_literal = '"usage": {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10},'
    assert fixture.count(usage_literal) == 1, 'shared Responses fixture changed'
    fixture = fixture.replace(usage_literal, '"usage": {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10, "input_tokens_details": {"cached_tokens": 3}, "output_tokens_details": {"reasoning_tokens": 2}},')
    mock_path = root / 'mock_provider.py'
    mock_path.write_text(fixture)
    lab.mock = lab.start('mock', MOCK_IMAGE, ('-B', '/mock_provider.py'),
                         env=('CLIPROXY_API_KEY',), entrypoint='python3',
                         extra=('--user', '1000:1000', '--read-only', '-v', str(FIXTURES) + ':/test:ro',
                                '-v', str(mock_path) + ':/mock_provider.py:ro'))
    config = values()['proxy_config']
    for model in config['model_list']:
        params = model['litellm_params']
        params['api_base'] = params['api_base'].replace('cliproxyapi.llm-gateway.svc.cluster.local', 'mock')
    config_path = root / 'config.yaml'
    config_path.write_text(yaml.safe_dump(config))
    config_path.chmod(0o644)
    env = ('DATABASE_URL', 'PROXY_MASTER_KEY', 'CLIPROXY_API_KEY', 'LITELLM_SALT_KEY',
           'LITELLM_LOCAL_MODEL_COST_MAP', 'STORE_MODEL_IN_DB')
    migration = lab.start('migration', IMAGE, ('/app/litellm/proxy/prisma_migration.py',),
                          env=env, entrypoint='python')
    assert lab.docker('wait', migration, timeout=180) == '0', 'official migration job failed'
    check('official nonroot migration initializes PostgreSQL', True)
    lab.proxy = lab.start('proxy', IMAGE, ('--config', '/app/config.yaml', '--port', '4000', '--num_workers', '1'),
                          env=env, extra=('-v', str(config_path) + ':/app/config.yaml:ro', '--memory', '4g'))
    lab.ready()
    check('official nonroot proxy ready with rendered model config', True)
    for key in (None, 'sk-invalid'):
        lab.request('/v1/models', key=key, expected=401)
    check('missing and invalid inference credentials denied', True)
    friend = lab.key('test-friend')
    info = lab.info(friend)
    check('native expiring model-scoped key with non-resetting budget',
          info['models'] == ['gpt-6-astra'] and info['budget_duration'] is None
          and info['expires'] is not None and info['max_budget'] == 1.0)
    catalog = lab.request('/v1/models', key=friend)
    check('friend model catalog scoped', {m['id'] for m in catalog['data']} == {'gpt-6-astra'})
    status, _ = lab.request('/key/generate', {'models': ['claude-haiku-5-5']}, friend, expected=None)
    check('inference key cannot create keys', status in (401, 403))
    body = {'model': 'gpt-6-astra', 'input': 'hello', 'max_output_tokens': 16, 'reasoning': {'effort': 'high'}}
    response = lab.request('/v1/responses', body, friend)
    check('Responses content and usage traverse actual LiteLLM', response['output'][0]['content'][0]['text'] == 'mock answer'
          and response['usage']['total_tokens'] == 10 and response['model'] == 'gpt-6-astra')
    check('cached and reasoning usage details retained', response['usage']['input_tokens_details']['cached_tokens'] == 3
          and response['usage']['output_tokens_details']['reasoning_tokens'] == 2)
    recorded = lab.request('/__requests', base='http://mock:8317')
    check('upstream authentication and model/effort fidelity', recorded[-1]['authorized']
          and recorded[-1]['body']['model'] == 'gpt-6-astra' and recorded[-1]['body']['reasoning']['effort'] == 'high')
    status, _ = lab.request('/v1/responses', {**body, 'model': 'claude-haiku-5-5'}, friend, expected=None)
    check('other model rejected before reaching upstream', status in (401, 403)
          and len(lab.request('/__requests', base='http://mock:8317')) == len(recorded))
    usage = lab.usage(friend, 0.000010)
    check('ten reported tokens charged as ten microcredits', abs(usage['spend'] - 0.000010) < 1e-10)
    stream = lab.request('/v1/responses', {**body, 'stream': True}, friend)
    events = [json.loads(line[6:]) for line in stream.splitlines() if line.startswith('data: ') and line[6:] != '[DONE]']
    check('Responses SSE reaches terminal event', events[-1]['type'] == 'response.completed'
          and events[-1]['response']['usage']['total_tokens'] == 10 and 'mock answer' in stream)
    lab.usage(friend, 0.000020)
    owner = lab.key('test-owner', models=['gpt-6-astra', 'claude-haiku-5-5'])
    chat = {'model': 'claude-haiku-5-5', 'messages': [{'role': 'user', 'content': 'hello'}], 'max_tokens': 16}
    message = lab.request('/v1/messages', chat, owner)
    check('native Anthropic content and usage', message['model'] == 'claude-haiku-5-5'
          and message['content'][0]['text'] == 'mock answer' and message['usage']['output_tokens'] == 3)
    stream = lab.request('/v1/messages', {**chat, 'stream': True}, owner)
    check('native Anthropic SSE reaches terminal event', 'event: message_stop' in stream and 'mock answer' in stream)
    chat_response = lab.request('/v1/chat/completions', {**chat, 'model': 'gpt-6-astra'}, friend)
    check('OpenAI Chat compatibility', chat_response['choices'][0]['message']['content'] == 'mock answer')
    lab.usage(friend, 0.000030)
    limited = lab.key('test-budget', max_budget=0.000005)
    lab.request('/v1/responses', body, limited)
    lab.usage(limited, 0.000010)
    before = len(lab.request('/__requests', base='http://mock:8317'))
    status, error = lab.request('/v1/responses', body, limited, expected=None)
    check('exhausted budget rejects before upstream', status == 422
          and 'budget' in json.dumps(error).lower()
          and len(lab.request('/__requests', base='http://mock:8317')) == before)
    original = lab.info(limited)
    lab.request('/key/update', {'key': limited, 'key_alias': 'test-budget-renamed'}, lab.env['PROXY_MASTER_KEY'])
    updated = lab.info(limited)
    check('key policy edit preserves usage and expiry', updated['spend'] == original['spend']
          and updated['expires'] == original['expires'] and updated['max_budget'] == original['max_budget'])
    rate = lab.key('test-rpm', rpm_limit=1)
    lab.request('/v1/responses', body, rate)
    lab.request('/v1/responses', body, rate, expected=429)
    check('per-key RPM limit enforced in single process', True)
    short = lab.key('test-expiry', duration='3s')
    lab.request('/v1/responses', body, short)
    time.sleep(3.2)
    status, _ = lab.request('/v1/responses', body, short, expected=None)
    check('native expiry rejects expired key', status in (401, 403))
    revoked = lab.key('test-revocation')
    lab.request('/key/delete', {'keys': [revoked]}, lab.env['PROXY_MASTER_KEY'])
    status, data = lab.request('/key/info?key=' + quote(revoked), key=lab.env['PROXY_MASTER_KEY'], expected=None)
    check('deleted key archived on exact readback', status == 200 and data['info']['status'] == 'deleted'
          and data['info']['deleted_at'] is not None)
    lab.request('/v1/models', key=revoked, expected=401)
    check('deleted key cannot authenticate', True)
    lab.docker('stop', '--time', '60', lab.proxy)
    check('proxy shuts down gracefully', lab.docker('inspect', '--format', '{{.State.ExitCode}}', lab.proxy) == '0')
    lab.docker('restart', '--time', '60', lab.pg)
    lab.docker('start', lab.proxy)
    lab.ready()
    persisted = lab.info(limited)
    check('Postgres and proxy restart preserve key budget and policy', persisted['spend'] == updated['spend']
          and persisted['models'] == updated['models'] and persisted['expires'] == updated['expires'])
    status, error = lab.request('/v1/responses', body, limited, expected=None)
    check('exhausted budget still rejects after restart', status == 422 and 'budget' in json.dumps(error).lower())
    lab.request('/v1/responses', body, friend)
    check('unexhausted key remains usable after restart', True)
    for params in ({'api_base': 'http://mock:8317/unapproved'}, {'extra_body': {'api_base': 'http://mock:8317/unapproved'}}):
        before = len(lab.request('/__requests', base='http://mock:8317'))
        status, _ = lab.request('/v1/responses', {**body, **params}, friend, expected=None)
        assert status in (400, 401, 403) and len(lab.request('/__requests', base='http://mock:8317')) == before
    check('client cannot redirect configured provider credentials', True)
    pricing_key = lab.key('test-pricing')
    status, _ = lab.request('/v1/responses', {**body, 'input_cost_per_token': 0, 'output_cost_per_token': 0}, pricing_key, expected=None)
    if status == 200:
        usage = lab.usage(pricing_key, 0.000010)
        assert abs(usage['spend'] - 0.000010) < 1e-10
    else:
        assert status in (400, 401, 403)
    check('client cannot zero configured usage pricing', True)


def main():
    lab = Lab()
    success = False
    try:
        with tempfile.TemporaryDirectory(prefix='litellm-test-') as tmp:
            root = Path(tmp)
            root.chmod(0o755)
            run(lab, root)
        success = True
    except Exception:
        for name in lab.containers:
            text = lab.docker('logs', '--tail', '35', name, required=False)
            for secret in PRIVATE:
                text = text.replace(secret, '[REDACTED]')
            if text:
                print(name + ':\n' + text[-3500:])
        raise
    finally:
        lab.cleanup()
        if path := os.environ.get('LITELLM_TEST_REPORT'):
            Path(path).write_text(json.dumps({'passed': success, 'checks': CHECKS,
                                             'image': IMAGE, 'postgres': PG_IMAGE,
                                             'upstream': 'isolated deterministic mock; no live providers'}, indent=2) + '\n')
    print(json.dumps({'passed': success, 'checks': len(CHECKS)}))


if __name__ == '__main__':
    def interrupted(_signum, _frame):
        raise KeyboardInterrupt('test interrupted')
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    main()
