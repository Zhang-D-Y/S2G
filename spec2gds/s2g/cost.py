"""Experiment-level currency ledger: reserve a worst-case cost before every paid request,
settle it from provider-reported usage afterwards.

A request is sent only if (settled actual cost + open reservations + this request's worst case)
stays within the ceiling. Worst case = (UTF-8 input bytes + overhead) tokens at the cache-miss
price plus every allowed output token, per transport attempt; a byte-level tokenizer never emits
more tokens than bytes. Settlement uses peak prices (an upper bound). A request whose outcome is
unknown keeps its full reservation as spent.
"""
import fcntl
from contextlib import contextmanager
from pathlib import Path
from .common import *

SCHEMA = 's2g.cost-ledger.v1'


def create(path, ceiling, pricing):
    for k in ('currency', 'input_cache_miss', 'input_cache_hit', 'output', 'per_tokens', 'source', 'checked_at',
              'overhead_tokens'):
        require(k in pricing, 'pricing missing ' + k)
    require(ceiling > 0, 'ceiling must be positive')
    require(not Path(path).exists(), 'cost ledger already exists')
    write(path, {'schema': SCHEMA, 'ceiling': ceiling, 'pricing': pricing, 'entries': {}, 'created_at': stamp()})


class Ledger:
    def __init__(self, path):
        self.path = Path(path)
        require(read(self.path).get('schema') == SCHEMA, 'not a cost ledger')

    @contextmanager
    def _locked(self):
        with self.path.with_suffix('.lock').open('a') as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            data = read(self.path)
            yield data
            write(self.path, data)

    def snapshot(self):
        return read(self.path)

    @staticmethod
    def committed(data):
        return round(sum(e['actual'] if e['status'] == 'SETTLED' else e['reserved'] for e in data['entries'].values()), 6)

    @staticmethod
    def worst_case(pricing, input_bytes, max_tokens, attempts):
        per = ((input_bytes + pricing['overhead_tokens']) * pricing['input_cache_miss'] + max_tokens * pricing['output'])
        return round(per * attempts / pricing['per_tokens'], 6)

    @staticmethod
    def actual(pricing, usage):
        hit = usage.get('prompt_cache_hit_tokens')
        miss = usage.get('prompt_cache_miss_tokens')
        if hit is None or miss is None:  # provider without a cache split: bill every input token as a miss
            hit, miss = 0, usage.get('prompt_tokens') or 0
        return (hit * pricing['input_cache_hit'] + miss * pricing['input_cache_miss']
                + (usage.get('completion_tokens') or 0) * pricing['output']) / pricing['per_tokens']

    def headroom(self):
        data = self.snapshot()
        return round(data['ceiling'] - self.committed(data), 6)

    def reserve(self, request_id, input_bytes, max_tokens, attempts, owner):
        """Idempotent per request id. Raises GateError if the ceiling would be exceeded."""
        with self._locked() as data:
            if request_id in data['entries']:
                return data['entries'][request_id]
            worst = self.worst_case(data['pricing'], input_bytes, max_tokens, attempts)
            require(self.committed(data) + worst <= data['ceiling'] + 1e-12,
                    f"currency ceiling: committed {self.committed(data)} + worst case {worst} > {data['ceiling']}")
            entry = {'status': 'RESERVED', 'reserved': worst, 'actual': None, 'owner': owner,
                     'input_bytes': input_bytes, 'reserved_at': stamp()}
            data['entries'][request_id] = entry
            return entry

    def settle(self, request_id, usages, attempts):
        """usages: one item per transport attempt (None when the provider returned no usage).
        Attempts without usage keep their share of the reservation (outcome unknown)."""
        with self._locked() as data:
            e = data['entries'][request_id]
            if e['status'] != 'RESERVED':
                return e
            share = e['reserved'] / max(attempts, 1)
            known = [u for u in usages if u]
            e['actual'] = round(sum(self.actual(data['pricing'], u) for u in known) + share * (attempts - len(known)), 6)
            e['usage'] = usages
            e.update(status='SETTLED', settled_at=stamp())
            return e

    def keep_unknown(self, request_id, reason):
        with self._locked() as data:
            e = data['entries'].get(request_id)
            if e and e['status'] == 'RESERVED':
                e.update(status='UNKNOWN_KEPT', reason=reason)
            return e
