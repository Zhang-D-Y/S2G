"""Frozen ALU proposal adapter. Rendering is generation, never correctness evidence.

The original RTL is annotated by its structural reading, not silently replaced by a
rendered origin. Free-form children lose the annotation. Typed parents are drawn only
from this loop's validated pool. Parent lineage and promotion incumbent are distinct.
"""
import json
import random
from pathlib import Path
from . import alu
from .common import GateError, digest, files_manifest, identity, require, tree_files, verify_manifest

HEADER = '# Candidate generation\n'
MODES = ('freeform', 'typed', 'hybrid')
STRATEGIES = ('random_single_action_walk', 'random_independent_sampling')


def freeze(policy, spec, rtl_policy, reference):
    g = policy.get('generation', {'mode': 'freeform'})
    require(g.get('mode') in MODES, 'unknown candidate generation mode')
    if g['mode'] == 'freeform':
        return None
    ir, v = g['ir'], g['vocabulary']
    alu.validate_ir(ir)
    require(v.get('schema') == 's2g.alu-actions.v1', 'unknown action vocabulary')
    require(set(v['dimensions']) == {'addsub_share', 'shifter', 'shift_style', 'select'}, 'unsupported dimensions')
    supported = {'shifter': {'separate', 'right_merged', 'reverse_right', 'reverse_left'},
                 'shift_style': {'native', 'stages_lsb_first', 'stages_msb_first'},
                 'select': {'case', 'and_or', 'grouped'}}
    for dim, values in supported.items():
        choices = v['dimensions'][dim]['values']
        require(isinstance(choices, list) and choices and len(set(choices)) == len(choices)
                and set(choices) <= values, 'unsupported vocabulary values: ' + dim)
    require(v['dimensions']['addsub_share']['of'] == list(alu.ADDER_OPS), 'unsupported adder vocabulary')
    ports = {p['name']: {'direction': 'input', 'width': p['width'], 'signed': False} for p in ir['op_ports']}
    ports.update({ir[n]: {'direction': 'output' if n == 'y' else 'input', 'width': ir['width'], 'signed': False}
                  for n in ('a', 'b', 'y')})
    require(ir['top'] == spec['top'] and ports == spec['ports'], 'ALU IR does not match frozen interface')
    require(spec.get('latency_cycles') == 0 and not spec.get('clocks') and not spec.get('resets'),
            'typed ALU scope is combinational only')
    require('$signed' in rtl_policy['allowed_system_functions'], 'ALU renderer requires pure signed casts')
    require(ir['origin_config'] in alu.space(ir, v), 'invalid golden structural annotation')
    require(type(g.get('max_edits_per_proposal')) is int and 1 <= g['max_edits_per_proposal'] <= 4,
            'max_edits_per_proposal must be 1..4')
    require(g.get('parent_policy') in ('incumbent', 'validated_typed_pool'), 'unsupported typed parent policy')
    return {'schema': 's2g.generation.v1', 'settings': g, 'reference_sha256': digest(reference),
            'preamble': alu.preamble_of(reference),
            'code': files_manifest(tree_files(Path(__file__).parent))}


def check(binding, reference):
    if binding:
        verify_manifest(binding['code'])
        require(digest(reference) == binding['reference_sha256'], 'typed reference changed')


def annotation(binding):
    return {'kind': 'golden_annotation', 'config': binding['settings']['ir']['origin_config'],
            'rtl_sha256': binding['reference_sha256']}


def context(binding, incumbent, parents, proposal, seen=()):
    g = binding['settings']
    return {'schema': 's2g.generation-context.v1', 'mode': g['mode'], 'proposal': proposal,
            'incumbent': incumbent, 'parent_policy': g['parent_policy'], 'parents': parents,
            'max_edits_per_proposal': g['max_edits_per_proposal'], 'ir': g['ir'], 'vocabulary': g['vocabulary'],
            'seen_configurations': sorted(set(seen))}


def prompt(ctx):
    text = (HEADER + json.dumps(ctx, sort_keys=True) + '\n\n'
            'Typed reply: {"action":"alu","parent":"<listed candidate id>",'
            '"edits":[{"op":"set","dimension":"<dimension>","value":"<valid value>"}],'
            '"hypothesis":"<falsifiable expected effect>"}. '
            'Use 1..max_edits_per_proposal edits, each on a different dimension. '
            'Only listed parents are allowed. The golden config is an annotation; rendering it is a charged proposal. '
            'Free-form candidates have no inferred config. All outputs undergo the same mandatory verification.')
    if ctx['mode'] == 'typed':
        text += ' This mode forbids action=submit; query and stop remain available.'
    return text


def decode(binding, obj, parents):
    require(set(obj) <= {'action', 'parent', 'edits', 'hypothesis'} and obj.get('action') == 'alu',
            'typed proposal has unknown fields')
    g = binding['settings']
    parent = obj.get('parent')
    require(isinstance(parent, str) and parent in parents, 'parent is not in the validated typed pool')
    edits = obj.get('edits')
    require(isinstance(edits, list) and 1 <= len(edits) <= g['max_edits_per_proposal'], 'typed edit count outside budget')
    dims, cfg = set(), parents[parent]['config']
    for e in edits:
        require(isinstance(e, dict) and isinstance(e.get('dimension'), str), 'malformed typed edit')
        require(e['dimension'] not in dims, 'a dimension may be changed only once per proposal')
        cfg = alu.apply(g['ir'], g['vocabulary'], cfg, e)
        dims.add(e['dimension'])
    text = alu.render(g['ir'], cfg, binding['preamble'])
    return text, {'kind': 'alu_rendered', 'parent': parent, 'config': cfg, 'edits': edits,
                  'primitive_edits': len(edits), 'rtl_sha256': identity_text(text)}


def identity_text(text):
    import hashlib
    return hashlib.sha256(text.encode()).hexdigest()


def random_proposal(ctx, strategy, seed):
    """Stateless deterministic control; reads structural context only, never evidence.

    Walk: one action from the synthesis incumbent. Independent sampling: a full config
    from the golden anchor (1..4 edits). Previously submitted typed configs are avoided
    when possible, including failed proposals; duplicates are still charged by the loop.
    """
    require(strategy in STRATEGIES, 'unknown random strategy')
    ir, v, parents = ctx['ir'], ctx['vocabulary'], ctx['parents']
    rng = random.Random(identity([seed, ctx['proposal']]))
    if strategy == STRATEGIES[0]:
        parent = ctx['incumbent']
        require(parent in parents, 'random walk needs a typed incumbent')
    else:
        parent = next((p for p, a in parents.items() if a['kind'] == 'golden_annotation'), None)
        require(parent is not None, 'independent sampler needs the golden anchor in the parent pool')
    cfg = parents[parent]['config']
    options = [c for c in alu.space(ir, v) if c != cfg and
               sum(c[d] != cfg[d] for d in cfg) <= (1 if strategy == STRATEGIES[0] else ctx['max_edits_per_proposal'])]
    fresh = [c for c in options if alu.config_id(c) not in ctx['seen_configurations']]
    require(options, 'random action space exhausted')
    target = rng.choice(fresh or options)
    return {'action': 'alu', 'parent': parent,
            'edits': [{'op': 'set', 'dimension': d, 'value': target[d]} for d in sorted(cfg) if cfg[d] != target[d]],
            'hypothesis': strategy + ': structural draw without reading metric values'}


class RandomTransport:
    """Offline controls use the same loop/validation/scheduling/selection as proposals."""
    live = False

    def __init__(self, strategy, seed):
        require(strategy in STRATEGIES and type(seed) is int, 'invalid random control')
        self.strategy, self.seed = strategy, seed

    def identity(self):
        return {'transport': 'random_alu_v1', 'model': 'offline-random', 'base_url': None,
                'paid': False, 'strategy': self.strategy, 'seed': self.seed}

    def send(self, request):
        user = request['messages'][1]['content']
        try:
            require(user.startswith(HEADER), 'random control needs a typed generation context')
            ctx = json.loads(user[len(HEADER):].split('\n', 1)[0])
            obj = random_proposal(ctx, self.strategy, self.seed)
            return {'http_status': 200, 'error': None, 'elapsed_s': 0.0,
                    'response': {'model': 'offline-random', 'choices': [{'message': {'content': json.dumps(obj)},
                                                                          'finish_reason': 'stop'}], 'usage': None}}
        except (GateError, KeyError, TypeError, ValueError) as e:
            return {'http_status': None, 'error': str(e), 'response': None, 'elapsed_s': 0.0}
