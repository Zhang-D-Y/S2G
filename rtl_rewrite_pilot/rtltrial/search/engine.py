"""Budgeted success/repair pools, structural proposals and completed-run feedback.

No network client: next writes a provider-ready request; submit accepts its
response. The deterministic demo exercises the same reserve/submit/evaluate path.
"""
import csv
import json
import math
import re
import shutil
from pathlib import Path

from rtltrial.liberty import load_liberty
from rtltrial.util import read_json, write_json, sha256_file, sha256_json, sha256_text, tool_version
from . import adapter, rules

SYSTEM = 'Return one JSON object containing a bounded RTL rewrite proposal. Do not call tools.'
MODEL = {'model': 'deepseek-flash', 'thinking': {'type': 'disabled'},
         'temperature': 1.0, 'max_tokens': 8192}
INPUT_LIMIT = 24000
FUNCTIONAL_KEYS = ('scope_status', 'lint', 'original_tb', 'extension_tb', 'formal')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def record(root, cid):
    return read_json(root / 'attempts' / cid / 'record.json')


def metrics(root, cid):
    if cid == 'B0':
        return read_json(root / 'baseline.json')['synth_repeats'][0]['metrics']
    return record(root, cid)['synth']['metrics']


def prepare(root, design, generation='structured', attempts=12, pool_size=4):
    require(not root.exists(), 'choose a new run directory')
    require(design in ('mul8', 'cla32'), 'unsupported design')
    require(generation in ('structured', 'freeform'), 'unsupported generation mode')
    require(type(attempts) is int and 1 <= attempts <= 100, 'attempt budget must be 1..100 per arm')
    require(type(pool_size) is int and 2 <= pool_size <= 8, 'pool size must be 2..8')
    e = adapter.load(design)
    source = adapter.PILOT / 'work/arithmetic_stability_v1' / f'{design}-rep1'
    old = e.verify(source)
    require(read_json(source / 'baseline.json')['status'] == 'BASELINE_OK', 'invalid source baseline')
    root.mkdir(parents=True)
    # Reuse immutable inputs, not measured candidate results. Fresh cold-start jobs below.
    shutil.copytree(source / 'frozen', root / 'frozen')
    (root / 'frozen/shared_prompt.txt').unlink()
    for cid in ('B0', 'NEG'):
        shutil.copytree(source / 'rtl' / cid, root / 'rtl' / cid)
    code = sorted(set(e.sources() + list(Path(__file__).parent.glob('*.py'))))
    hashes = {str(p.relative_to(adapter.PILOT)): sha256_file(p) for p in code}
    for p in code:
        dst = root / 'code_snapshot' / p.relative_to(adapter.PILOT)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(p, dst)
    man = {'schema': 'rtltrial.structure-search.v1', 'design': design, 'generation': generation,
           'source_contract': old['contract_hash'], 'source_run': str(source),
           'budget_per_arm': attempts, 'pool_size': pool_size, 'repair_pool_size': 2,
           'model': MODEL, 'max_input_bytes': INPUT_LIMIT,
           'arms': ['B1', 'B2'], 'rules': old['rules'],
           'tools': old['tools'], 'tool_versions': old['tool_versions'],
           'liberty': old['liberty'], 'liberty_sha256': old['liberty_sha256'],
           'generation_catalog': rules.catalog(design) if generation == 'structured' else [],
           'controller': 'best alternates with least-expanded structural alternatives; same in both arms',
           'validation': 'original TB + four-state extension + binary RTL proof + clean mapping; mapped proof for >10% baseline/parent area drop',
           'scope': 'unchanged arithmetic region and declarations; no e-graph saturation or general Verilog parser',
           'indirect_selection_leak': True, 'network_enabled': False,
           'code_hashes': hashes,
           'frozen_hashes': {str(p.relative_to(root)): sha256_file(p) for p in root.rglob('*') if p.is_file()}}
    man['contract_hash'] = sha256_json(man)
    write_json(root / 'protocol.json', man)
    for p in root.rglob('*'):
        if p.is_file():
            p.chmod(0o444)
    base = {'status': 'RUNNING', 'lint': e.screen.lint(root, 'B0', man['tools'])}
    write_json(root / 'baseline.json', base)
    base['original_tb'] = e.screen.simulate(root, 'B0', man['tools'])
    base['extension_tb'] = e.screen.simulate(root, 'B0', man['tools'], True)
    base['formal'] = e.screen.formal(root, 'B0', man['tools'])
    require(base['lint']['status'] == 'PASS' and base['original_tb']['status'] == 'TB_PASSED'
            and base['extension_tb']['status'] == 'TB_PASSED' and base['formal']['status'] == 'PASS', 'baseline validation failed')
    lib = load_liberty(man['liberty'], root / 'cache')
    ys = (root / 'frozen/synth.ys').read_text()
    base['synth_repeats'] = [e.screen.synth(root, 'B0', man['tools'], ys, lib, n) for n in (1, 2, 3)]
    require(all(s['status'] == 'SYNTH_DONE' and s['check_clean'] for s in base['synth_repeats']), 'baseline synthesis failed')
    require(len({(s['metrics']['area'], s['metrics']['netlist_sha256']) for s in base['synth_repeats']}) == 1,
            'baseline synthesis is not repeatable')
    base['negative_tb'] = e.screen.simulate(root, 'NEG', man['tools'])
    base['negative_formal'] = e.screen.formal(root, 'NEG', man['tools'])
    require(base['negative_tb']['status'] == 'TB_FAILED' and base['negative_formal']['status'] == 'FAIL', 'negative control not detected')
    base['status'] = 'BASELINE_OK'
    write_json(root / 'baseline.json', base)
    e.feedback(root, 'B0', base['synth_repeats'][0])
    state = {'status': 'PREPARED', 'baseline_sha256': sha256_file(root / 'baseline.json'), 'arms': {}}
    for arm in man['arms']:
        state['arms'][arm] = {'attempts': [], 'success_pool': ['B0'], 'repair_pool': [],
                              'best': 'B0', 'expansions': {}, 'pending': None}
    write_json(root / 'state.json', state)
    return {'status': 'PREPARED', 'contract_hash': man['contract_hash'], 'design': design, 'network_calls': 0}


def verify(root, require_current=True):
    man = read_json(root / 'protocol.json')
    require(sha256_json({k:v for k,v in man.items() if k != 'contract_hash'}) == man['contract_hash'], 'protocol changed')
    if require_current:
        for name, expected in man['code_hashes'].items():
            require(sha256_file(adapter.PILOT / name) == expected, 'runner changed: ' + name)
    for name, expected in man['frozen_hashes'].items():
        require(sha256_file(root / name) == expected, 'frozen input changed: ' + name)
    require(sha256_file(man['liberty']) == man['liberty_sha256'], 'Liberty changed')
    for name, binary in man['tools'].items():
        require(tool_version([binary, '-V']) == man['tool_versions'][name], 'tool changed: ' + name)
    state = read_json(root / 'state.json')
    require(sha256_file(root / 'baseline.json') == state['baseline_sha256'], 'baseline evidence changed')
    for arm in state['arms'].values():
        for item in arm['attempts']:
            if 'record_sha256' in item:
                require(sha256_file(root / 'attempts' / item['id'] / 'record.json') == item['record_sha256'], 'candidate record changed')
                r = record(root, item['id'])
                if r.get('prompt'):
                    require(sha256_json(read_json(root / 'attempts' / r['id'] / 'request.json')) == r['prompt']['request_sha256'], 'recorded request changed')
                if r.get('response_sha256'):
                    require(sha256_file(root / 'attempts' / r['id'] / 'response.txt') == r['response_sha256'], 'recorded response changed')
                if r.get('patch_sha256'):
                    require(sha256_file(root / 'rtl' / r['id'] / 'patch.diff') == r['patch_sha256'], 'candidate patch changed')
                if r.get('rtl_sha256'):
                    require(sha256_file(root / 'rtl' / r['id'] / 'dut.v') == r['rtl_sha256'], 'candidate source changed')
    return man, state


def structure(root, cid, design):
    return rules.initial(design) if cid == 'B0' else record(root, cid).get('structure')


def choose_parent(root, arm):
    n = len(arm['attempts'])
    if n % 2 == 0:
        return arm['best']
    # Neither branch exposes scores in the shared prompt; selection still leaks information.
    return min(arm['success_pool'], key=lambda cid: (arm['expansions'].get(cid, 0), cid))


def functional_view(r):
    proposal = json.dumps(r.get('proposal'), separators=(',', ':'))
    out = {'id': r['id'], 'proposal_preview': proposal[:1500], 'proposal_truncated': len(proposal) > 1500}
    for key in FUNCTIONAL_KEYS:
        value = r.get(key)
        out[key] = value.get('status') if isinstance(value, dict) else value
    return out


def checked_feedback(root, cid):
    fb = read_json(root / 'feedback' / (cid + '.json'))
    require(sha256_json({k:v for k,v in fb.items() if k != 'feedback_hash'}) == fb['feedback_hash'], 'feedback hash changed')
    require(sha256_file(root / 'rtl' / cid / 'dut.v') == fb['version_sha256'], 'feedback/source mismatch')
    result = root / fb['source_run'] / 'synth_result.json'
    require(sha256_file(result) == fb['source_result_sha256'], 'synthesis evidence changed')
    require(read_json(result)['status'] == 'SYNTH_DONE', 'feedback requires completed synthesis')
    require(read_json(result)['metrics'] == metrics(root, cid) == fb['metrics'], 'feedback metrics mismatch')
    return fb


def make_request(root, man, arm_name, arm, parent, index):
    design = man['design']
    prior = [record(root, x['id']) for x in arm['attempts']]
    history = [functional_view(r) for r in prior[-4:]]
    repair = [functional_view(record(root, cid)) for cid in arm['repair_pool']]
    common = {'task': 'Reduce mapped cell area. Preserve ports, declarations, widths, latency and binary/four-state behavior.',
              'contract': 'Only the selected arithmetic region may change. No new state, directives, attributes, X/Z literals or dont-cares. Every proposal including failures/duplicates consumes one slot. Verification is mandatory.',
              'attempt': index, 'budget': man['budget_per_arm'], 'generation': man['generation'],
              'parent': parent, 'rtl': (root / 'rtl' / parent / 'dut.v').read_text(),
              'original_tb': (root / 'frozen' / (design + '_test.sv')).read_text(),
              'recent_functional_history': history, 'repair_queue': repair,
              'selection_note': 'same area-based pool controller in both arms; parent choice conveys indirect synthesis information'}
    if man['generation'] == 'structured':
        common.update(response_schema={'op': 'operation name', 'params': 'operation-specific object'},
                      operations=man['generation_catalog'], parent_structure=structure(root, parent, design),
                      instruction='Choose a different structure from recent proposals; return only op and params. Rules propose alternatives and do not certify correctness.')
    else:
        e = adapter.load(design)
        common.update(response_schema={'block': 'replacement RTL region'}, selected_region=e.block_of(common['rtl']),
                      instruction='Return only block, <=6000 bytes; use existing signals only. Do not modify any other text.')
    shared = json.dumps(common, separators=(',', ':'))
    user, refs = shared, []
    if arm_name == 'B2':
        # Limit identically sized requests by a fixed recent-history window. No future jobs.
        ids = list(dict.fromkeys([parent] + [r['id'] for r in prior[-2:] if r.get('synth', {}).get('status') == 'SYNTH_DONE']))
        packages = []
        for cid in ids:
            fb = checked_feedback(root, cid)
            m = fb['metrics']
            item = {'id': cid, 'version_sha256': fb['version_sha256'],
                    'area': m['area'], 'cells': m['cells'], 'cells_by_type': m['cells_by_type'],
                    'logic_depth': m['logic_depth'], 'source_localization': fb['source_localization'][:4],
                    'warnings': fb['warnings'], 'timing': fb['timing']}
            if cid != 'B0':
                r = record(root, cid)
                item.update(parent=r['parent'], delta=r.get('delta'), collapse_to=r.get('collapse_to'))
            packages.append(item)
            refs.append({'id': cid, 'feedback_hash': fb['feedback_hash'], 'source_result_sha256': fb['source_result_sha256']})
        user += '\nSYNTHESIS_FEEDBACK_FROM_COMPLETED_RUNS\n' + json.dumps(packages, separators=(',', ':'))
    require(len((SYSTEM + user).encode()) <= man['max_input_bytes'], 'input limit exceeded; no slot reserved')
    request = {**man['model'], 'messages': [{'role':'system', 'content':SYSTEM}, {'role':'user', 'content':user}],
               'response_format': {'type':'json_object'}}
    return request, {'shared_sha256': sha256_text(shared), 'feedback_refs': refs,
                     'request_sha256': sha256_json(request), 'input_bytes': len((SYSTEM + user).encode())}


def reserve(root, arm_name):
    man, state = verify(root)
    require(arm_name in man['arms'], 'unknown arm')
    arm = state['arms'][arm_name]
    require(arm['pending'] is None, 'pending attempt must be submitted or aborted first')
    require(len(arm['attempts']) < man['budget_per_arm'], 'attempt budget exhausted')
    parent = choose_parent(root, arm)
    index = len(arm['attempts']) + 1
    cid = f'{arm_name}-{index:03d}'
    request, meta = make_request(root, man, arm_name, arm, parent, index)
    rec = {'id': cid, 'arm': arm_name, 'index': index, 'parent': parent, 'status':'RESERVED',
           'parent_sha256': sha256_file(root / 'rtl' / parent / 'dut.v'),
           'golden_sha256': sha256_file(root / 'rtl/B0/dut.v'), 'prompt':meta}
    arm['pending'] = cid
    arm['attempts'].append({'id': cid})
    arm['expansions'][parent] = arm['expansions'].get(parent, 0) + 1
    state['status'] = 'SEARCHING'
    # Persist reservation BEFORE the request can be delivered to a provider.
    write_json(root / 'state.json', state)
    write_json(root / 'attempts' / cid / 'record.json', rec)
    write_json(root / 'attempts' / cid / 'request.json', request)
    return rec


def evaluate(root, rec, man):
    e = adapter.load(man['design'])
    valid, normalized = e.check(rec['block'])
    rec['scope_status'] = 'PASS' if valid else 'FAIL'
    if not valid:
        rec.update(decision='REJECT', reason='scope: ' + normalized, repairable=True)
        return
    rec['normalized_hash'] = sha256_text(normalized)
    state = read_json(root / 'state.json')['arms'][rec['arm']]
    original = re.sub(r'//[^\n]*|/\*.*?\*/', '', e.block_of((root / 'rtl/B0/dut.v').read_text()), flags=re.S)
    seen = [('B0', sha256_text(e.check(original)[1]))]
    seen += [(x['id'], record(root, x['id']).get('normalized_hash')) for x in state['attempts'] if x['id'] != rec['id']]
    for cid, h in seen:
        if h == rec['normalized_hash']:
            rec.update(decision='DUPLICATE', reason='same token-normalized RTL region', duplicate_of=cid)
            return
    rec['rtl_sha256'] = e.materialize(root, rec['id'], rec['block'])
    rec['patch_sha256'] = sha256_file(root / 'rtl' / rec['id'] / 'patch.diff')
    for key, fn in [('lint', lambda:e.screen.lint(root, rec['id'], man['tools'])),
                    ('original_tb', lambda:e.screen.simulate(root, rec['id'], man['tools'])),
                    ('extension_tb', lambda:e.screen.simulate(root, rec['id'], man['tools'], True)),
                    ('formal', lambda:e.screen.formal(root, rec['id'], man['tools']))]:
        rec[key] = fn()
        want = 'TB_PASSED' if key.endswith('_tb') else 'PASS'
        if rec[key]['status'] != want:
            rec.update(decision='REJECT', reason=key + ' did not pass', repairable=True)
            return
    rec['synth'] = e.screen.synth(root, rec['id'], man['tools'], (root / 'frozen/synth.ys').read_text(),
                                   load_liberty(man['liberty'], root / 'cache'))
    sr = rec['synth']
    if sr['status'] != 'SYNTH_DONE':
        rec.update(decision='INCONCLUSIVE', reason='synthesis incomplete')
        return
    e.feedback(root, rec['id'], sr)
    cm, pm, bm = sr['metrics'], metrics(root, rec['parent']), metrics(root, 'B0')
    rec['delta'] = {'area': cm['area'] - pm['area'], 'area_relative': cm['area']/pm['area'] - 1,
                    'cells': cm['cells'] - pm['cells'], 'logic_depth': cm['logic_depth'] - pm['logic_depth'],
                    'cells_by_type': {k:cm['cells_by_type'].get(k,0)-pm['cells_by_type'].get(k,0)
                                      for k in sorted(cm['cells_by_type'].keys() | pm['cells_by_type'].keys())}}
    baseline = read_json(root / 'baseline.json')
    bs = baseline['synth_repeats'][0]
    new_lint = {tuple(w) for w in rec['lint']['warnings']} - {tuple(w) for w in baseline['lint']['warnings']}
    risks = ('coarse_ff_bits', 'coarse_latch_bits', 'mapped_ff_cells', 'mapped_latch_cells', 'undef_pin_bits')
    if (new_lint or not sr['check_clean'] or any(cm[k] for k in risks)
            or set(sr['warnings']) - set(bs['warnings']) or not math.isfinite(cm['area']) or cm['area'] <= 0):
        rec.update(decision='REJECT', reason='new warning or structural risk')
        return
    if max(1-cm['area']/pm['area'], 1-cm['area']/bm['area']) > man['rules']['suspicious_area_drop']:
        rec['large_drop_audit'] = e.gate_audit(root, rec['id'], man)
        if rec['large_drop_audit']['status'] != 'PASS':
            rec.update(decision='INCONCLUSIVE', reason='large drop requires mapped proof')
            return
    # Pool eligibility is distinct from the final area-improvement acceptance rule.
    rec['validated'] = True
    completed = ['B0'] + [x['id'] for x in state['attempts'] if x['id'] != rec['id'] and record(root,x['id']).get('validated')]
    for cid in completed:
        m = metrics(root, cid)
        if cm['fingerprint'] == m['fingerprint'] and cm['cells_by_type'] == m['cells_by_type']:
            rec.update(decision='COLLAPSED', collapse_to=cid,
                       reason='matching mapped structural fingerprint and cell distribution (heuristic, not equivalence proof)')
            return
    best = metrics(root, state['best'])
    gain = 1-cm['area']/best['area']
    rec.update(decision='KEEP' if gain >= man['rules']['minimum_area_improvement'] else 'EXPLORE',
               reason='verified improvement over incumbent' if gain >= man['rules']['minimum_area_improvement']
               else 'verified distinct implementation retained for exploration')


def update_pools(root, arm, rec, man):
    if rec.get('repairable'):
        arm['repair_pool'] = (arm['repair_pool'] + [rec['id']])[-man['repair_pool_size']:]
    if rec['decision'] not in ('KEEP', 'EXPLORE'):
        return
    if rec['decision'] == 'KEEP':
        arm['best'] = rec['id']
    candidates = list(dict.fromkeys(arm['success_pool'] + [rec['id'], arm['best']]))
    # Preserve best, then choose under-explored alternatives with different IR hashes.
    chosen, signatures = [arm['best']], set()
    def signature(cid):
        r = {} if cid == 'B0' else record(root,cid)
        return sha256_json(r.get('structure') or {'rtl':cid})
    signatures.add(signature(arm['best']))
    for cid in sorted(candidates, key=lambda c:(arm['expansions'].get(c,0), metrics(root,c)['area'],c)):
        sig = signature(cid)
        if cid not in chosen and sig not in signatures:
            chosen.append(cid); signatures.add(sig)
        if len(chosen) == man['pool_size']:
            break
    arm['success_pool'] = chosen


def submit(root, arm_name, response_text=None, error=None):
    man, state = verify(root)
    require(arm_name in man['arms'], 'unknown arm')
    arm = state['arms'][arm_name]
    cid = arm['pending']
    require(cid is not None, 'no reserved attempt')
    rec = record(root, cid)
    require(rec['status'] == 'RESERVED', 'attempt already started; abort interrupted attempt instead of retrying')
    require(sha256_file(root / 'rtl' / rec['parent'] / 'dut.v') == rec['parent_sha256'], 'parent changed')
    require(sha256_json(read_json(root / 'attempts' / cid / 'request.json')) == rec['prompt']['request_sha256'], 'request changed')
    rec['status'] = 'RUNNING'
    write_json(root / 'attempts' / cid / 'record.json', rec)
    try:
        if error is not None:
            raise ValueError('external generation failed: ' + error[:300])
        require(isinstance(response_text,str) and len(response_text.encode()) <= 65536, 'missing/oversize response')
        (root / 'attempts' / cid / 'response.txt').write_text(response_text)
        rec['response_sha256'] = sha256_file(root / 'attempts' / cid / 'response.txt')
        proposal = json.loads(response_text)
        # Accept direct proposal or a standard chat-completions response envelope.
        if isinstance(proposal, dict) and 'choices' in proposal:
            require(proposal['choices'][0].get('finish_reason') == 'stop', 'incomplete provider output')
            rec['usage'] = proposal.get('usage')
            rec['reported_model'] = proposal.get('model')
            proposal = json.loads(proposal['choices'][0]['message']['content'])
        rec['proposal'] = proposal
        if man['generation'] == 'structured':
            rec['structure'], rec['block'] = rules.apply(man['design'], structure(root,rec['parent'],man['design']), proposal)
            rec['structure_hash'] = sha256_json(rec['structure'])
        else:
            require(isinstance(proposal,dict) and set(proposal) == {'block'} and isinstance(proposal['block'],str), 'expected exactly block')
            rec['block'] = proposal['block']
        evaluate(root, rec, man)
    except Exception as ex:
        rec.update(decision='INCONCLUSIVE', reason=type(ex).__name__ + ': ' + str(ex)[:500],
                   repairable=not rec.get('rtl_sha256'))
    rec['status'] = 'COMPLETE'
    write_json(root / 'attempts' / cid / 'record.json', rec)
    update_pools(root, arm, rec, man)
    arm['attempts'][-1]['record_sha256'] = sha256_file(root / 'attempts' / cid / 'record.json')
    arm['pending'] = None
    write_json(root / 'state.json', state)
    return rec


def abort(root, arm_name):
    man, state = verify(root)
    arm = state['arms'][arm_name]
    require(arm['pending'] is not None, 'no pending attempt')
    cid = arm['pending']
    path = root / 'attempts' / cid / 'record.json'
    rec = read_json(path) if path.exists() else {'id':cid,'arm':arm_name,'index':len(arm['attempts'])}
    rec.update(status='COMPLETE', decision='INCONCLUSIVE', reason='interrupted/aborted; slot consumed, no retry')
    write_json(path, rec)
    arm['attempts'][-1]['record_sha256'] = sha256_file(path)
    arm['pending'] = None
    write_json(root / 'state.json', state)
    return rec


def summarize(root):
    # Read-only historical reporting verifies frozen snapshots, not current source.
    # Every mutating search operation still requires byte-identical current code.
    man, state = verify(root, require_current=False)
    base = metrics(root, 'B0')
    rows, arms = [], {}
    e = adapter.load(man['design'])
    jobs = e.tool_jobs(read_json(root / 'baseline.json'))
    for name, arm in state['arms'].items():
        counts = {}
        for x in arm['attempts']:
            r = record(root,x['id'])
            decision = r.get('decision',r['status'])
            counts[decision] = counts.get(decision,0)+1
            # Never recurse into untrusted proposal/usage/provider metadata.
            for stage in ('lint','original_tb','extension_tb','formal','synth','large_drop_audit'):
                jobs += e.tool_jobs(r.get(stage, {}))
            rows.append({'arm':name,'id':r['id'],'parent':r.get('parent'),'decision':decision,
                         'area':r.get('synth',{}).get('metrics',{}).get('area'), 'reason':r.get('reason')})
        arms[name] = {'attempts':len(arm['attempts']), 'pending':arm['pending'], 'decisions':counts,
                      'success_pool':arm['success_pool'], 'repair_pool':arm['repair_pool'],
                      'best':arm['best'], 'area':metrics(root,arm['best'])['area'],
                      'improvement_pct':100*(1-metrics(root,arm['best'])['area']/base['area'])}
    if (root / 'final.json').exists():
        jobs += e.tool_jobs(read_json(root / 'final.json'))
    out = {'schema':man['schema'], 'design':man['design'], 'generation':man['generation'], 'arms':arms,
           'runner_matches_current':all(sha256_file(adapter.PILOT/p)==h for p,h in man['code_hashes'].items()),
           'baseline_area':base['area'], 'final_audit':state.get('status'), 'tool_invocations':len(jobs), 'tool_wall_s':round(sum(j['elapsed_s'] for j in jobs),3),
           'network_calls_by_this_runner':0,
           'external_call_accounting':'reserved slots count failures; provider usage recorded when submitted, external billing not verified',
           'scope':'DEV search integration; deterministic proposals are not model gains or evidence of feedback benefit; no timing/P&R/power claim'}
    write_json(root / 'summary.json', out)
    if rows:
        with (root / 'attempts.csv').open('w', newline='') as f:
            w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
    return out


def finalize(root):
    man, state = verify(root)
    require(all(a['pending'] is None and len(a['attempts']) == man['budget_per_arm']
                for a in state['arms'].values()), 'finalization requires completed equal budgets')
    if state['status'] == 'COMPLETE':
        result = read_json(root / 'final.json')
        require(sha256_file(root / 'final.json') == state['final_sha256'], 'final audit changed')
        return result
    require(not (root / 'final.json').exists(), 'finalization already started; inspect retained logs, no implicit retry')
    e = adapter.load(man['design'])
    write_json(root / 'final.json', {'status':'RUNNING'})
    results = {}
    # Same best source across arms is still regressed independently.
    for name, arm in state['arms'].items():
        cid = name + '-final'
        parent = arm['best']
        if parent != 'B0':
            require(record(root,parent)['decision'] == 'KEEP', 'final candidate was not accepted')
        rd = root / 'rtl' / cid
        rd.mkdir(parents=True)
        shutil.copyfile(root / 'rtl' / parent / 'dut.v', rd / 'dut.v')
        r = {'source':parent, 'rtl_sha256':sha256_file(rd/'dut.v')}
        r['original_tb'] = e.screen.simulate(root,cid,man['tools'])
        r['extension_tb'] = e.screen.simulate(root,cid,man['tools'],True)
        r['formal'] = e.screen.formal(root,cid,man['tools'])
        ok = r['original_tb']['status'] == r['extension_tb']['status'] == 'TB_PASSED' and r['formal']['status'] == 'PASS'
        if parent != 'B0':
            lib = load_liberty(man['liberty'],root/'cache')
            ys = (root/'frozen/synth.ys').read_text()
            # The non-numbered run feeds the existing mapped-netlist audit helper.
            r['synth_repeats'] = [e.screen.synth(root,cid,man['tools'],ys,lib),
                                  e.screen.synth(root,cid,man['tools'],ys,lib,2)]
            ok = ok and all(s['status'] == 'SYNTH_DONE' and s['check_clean'] and
                           s['metrics']['area'] == metrics(root,parent)['area'] and
                           s['metrics']['netlist_sha256'] == metrics(root,parent)['netlist_sha256'] for s in r['synth_repeats'])
            if ok:
                r['mapped_formal'] = e.gate_audit(root,cid,man)
                ok = r['mapped_formal']['status'] == 'PASS'
        r['status'] = 'PASS' if ok else 'FAIL'
        results[name] = r
    result = {'status':'PASS' if all(r['status']=='PASS' for r in results.values()) else 'FAIL', 'arms':results}
    write_json(root/'final.json',result)
    state['status'] = 'COMPLETE' if result['status']=='PASS' else 'AUDIT_FAILED'
    state['final_sha256'] = sha256_file(root/'final.json')
    write_json(root/'state.json',state)
    summarize(root)
    return result
