"""Model transports for the optimization loop.

`OpenAICompatible` makes real (paid) chat-completion calls and is constructed only for an
explicit live run. `Scripted` replays prepared responses for tests and offline smokes.
Every physical request is recorded, including transport retries, which never create a new
proposal. Credentials are read from the environment, never written to disk.
"""
import json
import os
import time
import urllib.error
import urllib.request
from .common import *


class Scripted:
    """Offline transport: returns queued responses in order. Each item is either
    {'content': str} or {'error': str}; an exhausted script is a transport error."""
    live = False

    def __init__(self, items, model='scripted'):
        self.items, self.model = list(items), model

    def identity(self):
        return {'transport': 'scripted', 'model': self.model, 'base_url': None, 'paid': False}

    def send(self, request):
        start = time.monotonic()
        item = self.items.pop(0) if self.items else {'error': 'scripted responses exhausted'}
        resp = None if 'error' in item else {'id': f'scripted-{len(self.items)}', 'model': self.model,
                                             'choices': [{'message': {'content': item['content']}, 'finish_reason': 'stop'}],
                                             'usage': item.get('usage')}
        return {'http_status': None if 'error' in item else 200, 'error': item.get('error'), 'response': resp,
                'elapsed_s': round(time.monotonic() - start, 3)}


def live_identity(cfg):
    return {'transport': 'openai_compatible', 'model': cfg['model'], 'base_url': cfg['base_url'],
            'thinking': cfg.get('thinking'), 'paid': True}


class OpenAICompatible:
    live = True

    def __init__(self, cfg):
        self.cfg = cfg
        self.key = os.environ.get(cfg['api_key_env'])
        require(self.key, f"missing {cfg['api_key_env']} in the environment (e.g. `source ~/.llm_env`)")
        if cfg.get('model_env'):  # optional guard against a mis-sourced environment
            env_model = os.environ.get(cfg['model_env'], cfg['model'])
            require(env_model == cfg['model'], f"environment model {env_model!r} differs from frozen {cfg['model']!r}")

    def identity(self):
        return live_identity(self.cfg)

    def get(self, path):
        """Free account/metadata endpoints (model list, balance) for preflight."""
        url = self.cfg['base_url'].rstrip('/').removesuffix('/v1') + path
        req = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + self.key})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())

    def send(self, request):
        body = json.dumps(request).encode()
        req = urllib.request.Request(self.cfg['base_url'].rstrip('/') + '/chat/completions', data=body,
                                     headers={'Authorization': 'Bearer ' + self.key, 'Content-Type': 'application/json'})
        rec = {'http_status': None, 'error': None, 'response': None}
        start = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=self.cfg['timeout_s']) as r:
                rec['http_status'] = r.status
                rec['response'] = json.loads(r.read())
        except urllib.error.HTTPError as e:
            rec['http_status'] = e.code
            rec['error'] = f'HTTP {e.code}: ' + e.read().decode(errors='replace')[:500]
        except Exception as e:  # noqa: BLE001 - transport failure is evidence, not a crash
            rec['error'] = f'{type(e).__name__}: {str(e)[:300]}'
        rec['error'] = rec['error'] and rec['error'].replace(self.key, '[REDACTED]')
        rec['elapsed_s'] = round(time.monotonic() - start, 3)
        return rec


def build_request(cfg, system, user):
    req = {'model': cfg['model'], 'temperature': cfg['temperature'], 'max_tokens': cfg['max_tokens'],
           'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}]}
    if cfg.get('thinking') is not None:
        req['thinking'] = cfg['thinking']
    return req


def call(transport, cfg, system, user, request_dir, request_id):
    """One logical model request. Transport retries (cfg['transport_retries']) resend the
    identical payload under the same request_id; every physical attempt is written to disk
    before the next is made, and none of them creates an extra proposal."""
    request_dir = Path(request_dir); request_dir.mkdir(parents=True, exist_ok=True)
    payload = build_request(cfg, system, user)
    write(request_dir / 'request.json', {'request_id': request_id, 'payload_sha256': identity(payload),
                                         'transport': transport.identity(), 'payload': payload})
    attempts = []
    for n in range(1 + cfg.get('transport_retries', 0)):
        rec = transport.send(payload)
        rec.update(request_id=request_id, transport_attempt=n, completed_at=stamp())
        attempts.append(rec)
        write(request_dir / f'transport-{n}.json', rec)
        if rec['error'] is None:
            break
    last = attempts[-1]
    resp = last['response'] or {}
    choice = (resp.get('choices') or [{}])[0]
    out = {'request_id': request_id, 'transport_attempts': len(attempts), 'error': last['error'],
           'content': (choice.get('message') or {}).get('content'), 'finish_reason': choice.get('finish_reason'),
           'usage': [a['response'].get('usage') if a['response'] else None for a in attempts],
           'provider_ids': [a['response'].get('id') if a['response'] else None for a in attempts],
           'response_models': [a['response'].get('model') if a['response'] else None for a in attempts],
           'reasoning_content': bool((choice.get('message') or {}).get('reasoning_content')),
           'elapsed_s': round(sum(a['elapsed_s'] for a in attempts), 3)}
    write(request_dir / 'response.json', out)
    return out


def extract_json(text):
    """First decodable top-level JSON object in a reply (fenced or bare)."""
    if not text:
        return None
    dec = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch == '{':
            try:
                obj, _ = dec.raw_decode(text, i)
                if isinstance(obj, dict):
                    return obj
            except ValueError:
                continue
    return None
