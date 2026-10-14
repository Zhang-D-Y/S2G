"""Finite, resumable optimization loop around one frozen S2G workspace.

States: REGISTER_TASK -> VALIDATE_BASELINE -> BUILD_BASELINE_FEEDBACK -> REQUEST_CANDIDATE ->
FREEZE_CANDIDATE -> STATIC_CHECK -> FUNCTIONAL_CHECK -> SYNTHESIZE_AND_MEASURE ->
PHYSICAL_CHECK_IF_SCHEDULED -> BUILD_FEEDBACK -> DECIDE -> (REQUEST_CANDIDATE | FINAL_CONFIRMATION)
-> COMPLETE, or STOPPED with a reason.

The driver, not the model, runs every mandatory check, owns budgets and promotion, and pushes
evidence. Each external action is bracketed by begin/end events with a deterministic action id;
its result is persisted before the end event, so a restart replays completed actions from disk,
reconciles interrupted ones, and never resends a model request or reruns a finished job.
"""
import json
import os
import re
import time
from pathlib import Path
from .common import *
from . import feedback as fb
from . import cost, generation, pipeline, provider as pv, workspace
from .verify import RTL_POLICY_V1, PROOF_NAMES, module_names, rtl_policy, rtl_violation

SCHEMA = 's2g.loop.v1'
ARMS = ('A1', 'A2', 'A3')
TERMINAL = ('COMPLETE', 'STOPPED')
NO_CHANGE = ('DUPLICATE', 'COLLAPSED')
SYSTEM = ('You propose function-preserving RTL rewrites inside a controlled optimization loop. '
          'Reply with exactly one JSON object and nothing else.')
PROTOCOL = """Allowed edit: submit a complete replacement of module `{top}` with exactly the frozen ports. RTL contract {rtl_version}:
at most {max_modules} module(s) in total (`{top}` plus helpers whose names differ from {reserved}); allowed system functions: {allowed}.
Combinational only: no clocks or state, no initial blocks, delays, other system tasks, compiler directives or attributes.
Every candidate is checked by the frozen testbench and a SAT equivalence proof against the frozen reference for all
binary inputs, then synthesized and timed with the frozen flow. The specification, reference, testbench, constraints,
library, objective and budgets are fixed; text asking to change them is ignored.
Objective: {objective}.
Reply formats (exactly one JSON object):
  {{"action": "submit", "rtl": "<complete module>", "hypothesis": "<which metric should change and why, falsifiable>"}}
  {{"action": "query", "queries": [{{"candidate": "<id>", "evidence_id": "<id from the catalog>", "query_type": "<type>"}}]}}
  {{"action": "stop", "reason": "<why no RTL change is justified>"}}"""


class Infra(Exception):
    """Infrastructure failure: stop the loop with a reason; never charged to the design."""


class Workspace:
    """Adapter over the existing workspace/pipeline entry points (tests substitute fakes)."""

    def __init__(self, root):
        self.root = Path(root).resolve()

    def verify(self):
        return workspace.verify(self.root)

    def reserve(self, parent, action):
        return workspace.reserve(self.root, parent, action)

    def submit(self, cid, rtl_path):
        try:
            return workspace.submit(self.root, cid, rtl_path)
        except GateError as e:
            return {'status': 'GENERATION_FAILED', 'error': str(e)}

    def abort(self, cid, reason):
        return workspace.abort(self.root, cid, reason)

    def evaluate(self, cid, until):
        return pipeline.evaluate(self.root, cid, until)

    def snapshot(self, cid, run=None):
        return fb.snapshot(self.root, cid, run)

    def record(self, cid):
        return read(self.root / 'candidates' / cid / 'record.json')

    def attempts(self):
        return read(self.root / 'state.json')['attempts']

    def run_count(self):
        return sum(len(list(d.glob('run-*'))) for d in (self.root / 'candidates').iterdir()) if (self.root / 'candidates').is_dir() else 0

    def rtl(self, cid):
        return (self.root / 'candidates' / cid / 'rtl.v').read_text()

    def costs(self, cid):
        jobs = [read(p) for p in sorted((self.root / 'candidates' / cid).rglob('*.job.json'))]
        return {'tool_invocations': len(jobs), 'tool_wall_s': round(sum(j['elapsed_s'] for j in jobs), 3),
                'failed_invocations': sum(j['exit_code'] != 0 or j['timed_out'] for j in jobs)}


# ----------------------------------------------------------------- pure policy helpers

def static_check(text, top, max_chars, policy=None, reserved=()):
    """Cheap pre-tool check using the same RTL contract as the functional gate."""
    if len(text) > max_chars:
        return f'candidate exceeds {max_chars} chars'
    return rtl_violation(text, top, policy or RTL_POLICY_V1, reserved)


def normalized(text):
    return re.sub(r'\s+', ' ', re.sub(r'/\*.*?\*/|//[^\n]*', '', text, flags=re.S)).strip()


def metric_value(snap, key):
    m = ((snap or {}).get('synthesis') or {}).get('metrics', {}).get(key) or {}
    return m.get('value') if m.get('status') == 'VALID' else None


def decide(policy, cand, inc):
    """Predeclared synthesis-stage promotion rule, identical for every arm."""
    obj = policy['objective']
    c_ev, i_ev = cand.get('synthesis'), inc.get('synthesis')
    area, inc_area, ws = metric_value(cand, 'mapped_area'), metric_value(inc, 'mapped_area'), metric_value(cand, 'setup_ws')
    rel = (inc_area - area) / inc_area if area is not None and inc_area else None
    gates = {'run_complete': cand['status'] == 'COMPLETE', 'functional': cand['functional'] == 'PASS',
             'mapped_equivalence': cand['mapped_proof'] == 'PASS', 'synthesis_evidence': c_ev is not None,
             'mapping_complete': bool(c_ev) and c_ev['mapping']['status'] == 'COMPLETE',
             'area_valid': area is not None and inc_area is not None,
             'timing_valid': ws is not None, 'setup_limit': ws is not None and ws >= obj['setup_ws_min_ns'],
             'structural_change': bool(c_ev and i_ev) and c_ev['structure']['fingerprint'] != i_ev['structure']['fingerprint'],
             'area_improved': rel is not None and rel >= obj['min_rel_area_improvement']}
    order = list(gates)
    failed = next((g for g in order if not gates[g]), None)
    if failed is None:
        outcome = 'PROMOTED'
    elif failed == 'structural_change':
        outcome = 'COLLAPSED'
    elif failed == 'area_improved':
        outcome = 'NO_GAIN'
    elif failed in ('area_valid', 'timing_valid', 'synthesis_evidence'):
        outcome = 'UNKNOWN_METRIC'
    else:
        outcome = 'REJECTED_GATE'
    return {'outcome': outcome, 'promote': failed is None, 'first_failed_gate': failed, 'gates': gates,
            'objective': {'kind': obj['kind'], 'area_rel_improvement_vs_incumbent': None if rel is None else round(rel, 6), 'candidate_area': area,
                          'incumbent_area': inc_area, 'candidate_setup_ws_ns': ws}}


def final_label(policy, inc, base, conf):
    """Final status from completed confirmation evidence only. ACCEPTED_IMPLEMENTATION needs:
    the required number of repeat syntheses for winner and baseline, each with an unchanged
    fingerprint and a VALID area; final physical runs for both with OpenRCX-extracted
    parasitics; and routed acceptance of the winner. Anything missing is listed, never assumed."""
    selection_missing = conf.get('selection', {}).get('unmet_requirements', [])
    if inc == base and policy.get('final_selection', {}).get('kind') != 'routed_pool' and not selection_missing:
        return 'ORIGINAL_RETAINED', []
    missing = list(selection_missing)
    need = policy['final_confirmation']['repeat_synthesis']
    for role, cid in (('winner', inc), ('baseline', base)):
        reps = conf.get('repeats', {}).get(cid, [])
        if len(reps) < need:
            missing.append(f'repeat synthesis of {role} {cid}: {len(reps)}/{need}')
        if any(not r.get('same_fingerprint') or r.get('area') is None for r in reps):
            missing.append(f'repeat synthesis of {role} {cid} not reproducible or area not VALID')
        phys = conf.get('physical', {}).get(cid, {})
        if not policy['physical']['final']:
            missing.append('final physical confirmation disabled by policy')
        elif phys.get('acceptance') is None:
            missing.append(f'final physical run of {role} {cid}: {phys.get("status", "NOT_RUN")}')
        elif phys.get('parasitics') != 'detailed_route_extraction':
            missing.append(f'{role} {cid} parasitics are {phys.get("parasitics")}, not detailed_route_extraction')
    winner = conf.get('physical', {}).get(inc, {}).get('acceptance')
    if winner == 'REJECTED':
        return 'PHYSICALLY_REJECTED', sorted(set(missing))
    if winner == 'ACCEPTED' and not missing:
        return ('ORIGINAL_RETAINED' if inc == base else 'ACCEPTED_IMPLEMENTATION'), []
    return 'SYNTHESIS_PROVISIONAL', sorted(set(missing))


def routed_pool_selection(policy, baseline, pool):
    """Post-search selection under a NEW frozen policy, identical in A2 and A3.

    Completed physical failures remain rejected observations. Missing runs/accepted
    measurements make the comparison provisional. Negative synthesis slack does not
    exclude a functionally and mapped-equivalent routed implementation.
    """
    b = pool[baseline]
    rows, missing = {}, []
    for cid, s in pool.items():
        sy = s.get('synthesis') or {}
        if not (s['functional'] == 'PASS' and s['mapped_proof'] == 'PASS'
                and sy.get('mapping', {}).get('status') == 'COMPLETE'):
            continue
        p = s.get('physical') or {}
        risk = p.get('risk') or {}
        area, slack = risk.get('area_um2'), worst_slack(risk, 'setup')
        accepted = s['status'] == 'COMPLETE' and p.get('acceptance', {}).get('status') == 'ACCEPTED'
        extracted = risk.get('parasitics', {}).get('provenance') == 'detailed_route_extraction'
        if s.get('physical_scheduled_run_missing') or (not p and s['status'] != 'FAILED'):
            missing.append('routed pool coverage missing: ' + cid)
        elif p.get('acceptance', {}).get('status') not in ('ACCEPTED', 'REJECTED') and s['status'] != 'FAILED':
            missing.append('routed pool acceptance missing: ' + cid)
        elif accepted and (not extracted or area is None or area <= 0 or slack is None):
            missing.append('accepted routed pool metrics/extraction missing: ' + cid)
        rows[cid] = {'accepted': accepted, 'area_um2': area, 'setup_ws_ns': slack,
                     'extracted': extracted, 'run': s['run'], 'run_status': s['status']}
    br = rows.get(baseline, {})
    if not br.get('accepted') or not br.get('extracted') or not br.get('area_um2'):
        missing.append('routed baseline not qualified for pool selection')
        return {'winner': baseline, 'rows': rows, 'unmet_requirements': sorted(set(missing))}
    threshold = policy['final_selection']['min_rel_area_improvement']
    eligible = []
    for cid, r in rows.items():
        structural = pool[cid]['synthesis']['structure']['fingerprint'] != b['synthesis']['structure']['fingerprint']
        gain = (br['area_um2'] - r['area_um2']) / br['area_um2'] if r['area_um2'] is not None else None
        r['gain_rel'] = gain
        if cid != baseline and r['accepted'] and r['extracted'] and structural and gain is not None and gain >= threshold:
            eligible.append(cid)
    winner = min(eligible, key=lambda c: (rows[c]['area_um2'], c)) if eligible else baseline
    return {'winner': winner, 'rows': rows, 'unmet_requirements': sorted(set(missing))}


# ----------------------------------------------------------------- the loop

class Loop:
    def __init__(self, root, loop_id, transport=None, ws=None):
        self.root = Path(root).resolve()
        self.dir = self.root / 'loops' / loop_id
        self.ws = ws or Workspace(self.root)
        self.transport = transport
        self.man = read(self.dir / 'manifest.json')
        self.state = read(self.dir / 'state.json')
        self.policy = self.man['policy']
        self.events = self.dir / 'events.jsonl'

    # ---- persistence
    def event(self, kind, **data):
        with self.events.open('a') as f:
            f.write(json.dumps({'at': stamp(), 'event': kind, **data}, sort_keys=True) + '\n')
            f.flush(); os.fsync(f.fileno())

    def save(self):
        write(self.dir / 'state.json', self.state)

    def goto(self, state, **why):
        self.event('transition', frm=self.state['state'], to=state, **why)
        self.state['state'] = state
        self.save()

    def ledger(self):
        return cost.Ledger(self.man['cost_ledger']) if self.man.get('cost_ledger') else None

    def rtl_contract(self):
        _, ctx, _ = self.ws.verify()
        tb = self.root / 'frozen/tb.v'
        return rtl_policy(ctx), module_names(tb.read_text()) if tb.is_file() else []

    def logged(self):
        if not self.events.is_file():
            return {}, {}
        begun, ended = {}, {}
        for line in self.events.read_text().splitlines():
            e = json.loads(line)
            if e['event'] == 'begin':
                begun[e['action_id']] = e
            elif e['event'] == 'end':
                ended[e['action_id']] = e
        return begun, ended

    def action(self, action_id, fn, recover=None):
        """Run `fn` once per action id. Completed -> cached result; begun but not ended ->
        `recover` decides from durable outputs (never a blind re-execution of paid/long work)."""
        path = self.dir / 'actions' / (re.sub(r'[^A-Za-z0-9_.-]', '_', action_id) + '.json')
        begun, ended = self.logged()
        if action_id in ended:
            return read(path)
        if action_id in begun:
            result = recover(begun[action_id]) if recover else None
            if result is None:
                result = fn()
            else:
                result['reconciled'] = True
        else:
            self.event('begin', action_id=action_id)
            t0 = time.monotonic()
            result = fn()
            self.state['elapsed_s'] = round(self.state['elapsed_s'] + time.monotonic() - t0, 3)
        write(path, result)
        self.save()
        self.event('end', action_id=action_id, status=result.get('status'), reconciled=result.get('reconciled', False))
        return result

    # ---- budgets
    def usage(self):
        """Request/query usage derived from attempt records (including the in-flight one), so a
        replay after a crash recounts exactly instead of double-counting."""
        u = dict.fromkeys(('model_requests', 'transport_requests', 'query_calls', 'query_chars', 'prompt_chars',
                           'prompt_tokens', 'completion_tokens'), 0)
        for a in self.state['attempts'] + ([self.state['inflight']] if self.state.get('inflight') else []):
            for r in a['requests']:
                u['model_requests'] += 1
                for k in ('transport_requests', 'prompt_chars', 'prompt_tokens', 'completion_tokens'):
                    u[k] += r[k]
            for q in a['queries']:
                u['query_calls'] += 1; u['query_chars'] += q['chars']
        return u

    def remaining(self):
        b, c, u = self.policy['budget'], self.state['counters'], self.usage()
        return {'candidate_submissions': b['candidate_submissions'] - len(self.state['attempts']),
                'model_requests': b['model_requests'] - u['model_requests'],
                'query_calls': b['query_calls'] - u['query_calls'], 'query_chars': b['query_chars'] - u['query_chars'],
                'physical_evaluations': b['physical_evaluations'] - c['physical_evaluations'],
                'wall_s': round(b['wall_s'] - self.state['elapsed_s'], 1)}

    def stop_reason(self):
        r = self.remaining()
        for key in ('candidate_submissions', 'model_requests', 'wall_s'):
            if r[key] <= 0:
                return 'budget_exhausted:' + key
        _, ctx, _ = self.ws.verify()
        if len(self.ws.attempts()) >= ctx['candidate_budget']:
            return 'budget_exhausted:workspace_candidate_budget'
        if self.ws.run_count() >= ctx.get('tool_run_budget', ctx['candidate_budget'] * 3):
            return 'budget_exhausted:workspace_tool_run_budget'
        if self.state['consecutive_no_change'] >= self.policy['stop']['max_consecutive_no_change']:
            return 'repeated_no_change'
        return None

    # ---- evidence
    def snap(self, cid, run):
        try:
            return self.ws.snapshot(cid, run)
        except GateError as e:
            self.state['counters']['stale_evidence_rejections'] += 1
            raise Infra(f'evidence for {cid}/{run} failed verification: {e}') from e

    def evaluate(self, cid, until):
        aid = f'evaluate:{cid}:{until}:{len(self.state["candidates"].get(cid, {}).get("runs", []))}'

        def run():
            try:
                r = self.ws.evaluate(cid, until)
            except GateError as e:
                raise Infra(f'evaluation of {cid} could not start: {e}') from e
            return {'status': r['status'], 'run': Path(r['run']).name, 'error': r.get('error'),
                    'acceptance': r['acceptance']['status']}

        def recover(begin):
            rec = self.ws.record(cid)
            if rec['status'] == 'RUNNING':
                raise Infra(f'{cid} was interrupted while RUNNING; the workspace requires a new run root')
            known = self.state['candidates'].get(cid, {}).get('runs', [])
            if rec.get('last_run') and rec['last_run'] not in known:
                return {'status': rec['status'], 'run': rec['last_run'], 'error': None, 'acceptance': None}
            return None
        r = self.action(aid, run, recover)
        runs = self.state['candidates'].setdefault(cid, {'runs': []})['runs']
        if r['run'] not in runs:
            runs.append(r['run'])
        return r

    # ---- main entry
    def run(self, max_steps=10_000):
        self.check_identity()
        steps = 0
        while self.state['state'] not in TERMINAL and steps < max_steps:
            steps += 1
            handler = getattr(self, 'on_' + self.state['state'].lower())
            try:
                handler()
            except Infra as e:
                self.stop(f'infrastructure_failure: {e}', final=False)
        if self.state['state'] in TERMINAL:
            self.summarize()
        return self.state

    def check_identity(self):
        spec, ctx, freeze = self.ws.verify()
        require(digest(self.root / 'freeze.json') == self.man['workspace_freeze_sha256'], 'workspace freeze changed; start a new loop')
        require(identity(freeze['tools']) == self.man['toolchain_id'], 'toolchain changed; start a new loop')
        require(identity(self.policy) == self.man['policy_sha256'], 'policy changed; start a new loop')
        require(identity({k: v for k, v in self.man.items() if k != 'manifest_sha256'}) == self.man['manifest_sha256'],
                'loop manifest was modified')
        generation.check(self.man.get('generation'), self.root / 'frozen/reference.v')
        if self.man.get('cost_ledger'):
            require(identity(read(self.man['cost_ledger'])['pricing']) == self.man['pricing_sha256'], 'cost ledger pricing changed')
        elif self.transport is not None and getattr(self.transport, 'live', False):
            raise GateError('live transport requires a loop frozen with a cost ledger')
        if self.state['state'] not in TERMINAL:
            require(self.transport is not None, 'a transport is required to run this loop')
            require(self.transport.identity() == self.man['provider'], 'transport differs from the frozen provider identity')

    def stop(self, reason, final=True):
        self.state['stop_reason'] = reason
        self.event('stop', reason=reason)
        self.goto('FINAL_CONFIRMATION' if final else 'STOPPED', reason=reason)

    # ---- states
    def on_register_task(self):
        self.goto('VALIDATE_BASELINE')

    def on_validate_baseline(self):
        base = self.man['baseline']
        info = self.state['candidates'].get(base, {})
        if not info.get('synth_run'):
            r = self.evaluate(base, 'synthesis')
            self.state['candidates'][base]['synth_run'] = r['run']
        run = self.state['candidates'][base]['synth_run']
        snap = self.snap(base, run)
        if self.man.get('generation'):
            self.state['candidates'][base]['generation'] = generation.annotation(self.man['generation'])
        d = decide(self.policy, snap, snap)
        problems = [g for g in ('run_complete', 'functional', 'mapped_equivalence', 'synthesis_evidence',
                                'mapping_complete', 'area_valid', 'timing_valid') if not d['gates'][g]]
        self.state['baseline_qualification'] = {'gates': {g: d['gates'][g] for g in d['gates'] if g not in
                                                          ('structural_change', 'area_improved')},
                                                'problems': problems, 'run': run}
        if problems:
            self.stop('baseline_not_qualified: ' + ', '.join(problems), final=False)
            return
        if self.policy['physical'].get('baseline', False):
            # Both arms pay for the same baseline measurement. Exposure controls
            # whether the proposer sees it, independently of execution/selection.
            if not self.state['candidates'][base].get('phys_run'):
                if self.remaining()['physical_evaluations'] <= 0:
                    self.stop('baseline_not_qualified: physical budget exhausted', final=False)
                    return
                self.physical(base)
            phys = self.snap(base, self.state['candidates'][base]['phys_run'])
            status = (phys.get('physical') or {}).get('acceptance', {}).get('status')
            self.state['baseline_qualification']['physical'] = {
                'run': phys['run'], 'acceptance': status, 'run_status': phys['status']}
            if phys['status'] != 'COMPLETE' or status != 'ACCEPTED':
                self.stop('baseline_not_qualified: routed acceptance', final=False)
                return
        self.goto('BUILD_BASELINE_FEEDBACK')

    def on_build_baseline_feedback(self):
        base = self.man['baseline']
        self.state['incumbent'] = base
        self.state['incumbent_history'].append({'candidate': base, 'reason': 'baseline'})
        self.goto('REQUEST_CANDIDATE')

    def evidence_snapshot(self, cid):
        info = self.state['candidates'][cid]
        run = info.get('phys_run') if self.man['arm'] == 'A3' and info.get('phys_run') else info['synth_run']
        return self.snap(cid, run)

    def recent_text(self):
        arm, cap = self.man['arm'], self.policy['feedback']['recent_attempt_chars']
        lines = []
        for a in self.state['attempts'][-self.policy['feedback']['recent_attempts']:]:
            s = f"- {a['proposal']}: candidate {a.get('candidate') or '-'} rtl {str(a.get('rtl_sha256'))[:12]}; " \
                f"hypothesis: {str(a.get('hypothesis'))[:200]}; outcome: {a['correctness']}"
            if arm != 'A1' and a.get('synthesis_note'):
                s += '; ' + a['synthesis_note']
            s += f"; promoted: {'yes' if a.get('promoted') else 'no'}"
            lines.append(s)
        text = '\n'.join(lines) or '(none)'
        return text if len(text) <= cap else text[-cap:]

    def typed_parents(self):
        binding = self.man.get('generation')
        if not binding:
            return {}
        parents = {}
        for cid, info in self.state['candidates'].items():
            a = info.get('generation')
            if not a or not info.get('synth_run'):
                continue
            if binding['settings']['parent_policy'] == 'incumbent' and cid != self.state['incumbent']:
                continue
            # Parent qualification never reads physical evidence (same candidate pool for A2/A3).
            s = self.snap(cid, info['synth_run'])
            if s['functional'] == 'PASS' and s['mapped_proof'] == 'PASS' and s['status'] == 'COMPLETE':
                require(generation.identity_text(self.ws.rtl(cid)) == a['rtl_sha256'], 'typed parent RTL binding changed')
                parents[cid] = a
        return parents

    def generation_context(self):
        binding = self.man.get('generation')
        if not binding:
            return None
        seen = [generation.alu.config_id(a['generation']['config']) for a in self.state['attempts'] if a.get('generation')]
        return generation.context(binding, self.state['incumbent'], self.typed_parents(),
                                  len(self.state['attempts']) + 1, seen)

    def prompt(self, packet_text, extra=''):
        spec, _, _ = self.ws.verify()
        inc = self.state['incumbent']
        contract = {k: spec[k] for k in ('top', 'ports', 'behavior', 'arithmetic', 'legal_inputs', 'requirements')}
        obj = self.policy['objective']
        objective = (f"{obj['kind']}: reduce mapped area by >= {obj['min_rel_area_improvement']:.2%} relative to the "
                     f"incumbent while setup worst slack >= {obj['setup_ws_min_ns']} ns under the frozen constraints")
        pol, reserved = self.rtl_contract()
        head = '# Contract\n' + json.dumps(contract, sort_keys=True) + '\n' + PROTOCOL.format(
            top=spec['top'], objective=objective, rtl_version=pol['version'], max_modules=pol['max_modules'],
            reserved=sorted(set(reserved) | PROOF_NAMES), allowed=', '.join(pol['allowed_system_functions']) or 'none')
        if self.policy.get('experiment', {}).get('goal_text'):
            head += '\nFinal evaluation: ' + self.policy['experiment']['goal_text']
        require(len(head) <= self.policy['feedback']['contract_chars'], 'contract exceeds its frozen cap')
        rtl = self.ws.rtl(inc)
        require(len(rtl) <= self.policy['feedback']['max_rtl_chars'],
                'incumbent RTL does not fit; a bounded edit closure is required (unsupported in this version)')
        catalog = {}
        for c in self.state['last_catalog']:
            catalog.setdefault(c['candidate'], {}).setdefault(c['query_type'], []).append(c['evidence_id'])
        catalog = {'ids_by_candidate_and_query_type': catalog, 'rtl_region_ids': 'rtl or rtl:<first>-<last>',
                   'default_candidate': self.state['incumbent']}
        parts = [
            head, f"# Remaining budget\n{json.dumps(self.remaining(), sort_keys=True)}",
            f'# Incumbent {inc} (complete RTL)\n```verilog\n{rtl}\n```',
            '# Evidence (pushed automatically)\n' + packet_text,
            '# Recent attempts (summaries; full records are kept outside this prompt)\n' + self.recent_text(),
            '# Query catalog (optional; each query costs budget)\n' + json.dumps(catalog, sort_keys=True), extra]
        ctx = self.generation_context()
        if ctx:
            gen_text = generation.prompt(ctx)
            require(len(gen_text) <= self.policy['feedback'].get('generation_chars', 16000), 'generation context exceeds frozen cap')
            parts.insert(0, gen_text)
        return '\n\n'.join(parts).strip()

    def recent_candidate(self):
        """Most recently evaluated candidate that is not the incumbent (failed or unpromoted)."""
        for a in reversed(self.state['attempts']):
            cid = a.get('candidate')
            if cid and a.get('outcome') != 'DUPLICATE' and cid != self.state['incumbent'] \
                    and self.state['candidates'].get(cid, {}).get('synth_run'):
                return cid
        return None

    def on_request_candidate(self):
        last = self.state['attempts'][-1] if self.state['attempts'] else None
        if last is not None and last.get('outcome') is None:
            # An attempt recorded by an older state commit but never routed onward: finish it first,
            # because its slot is already spent and a returned candidate must still be verified.
            if last['correctness'] == 'MODEL_STOPPED':
                last['outcome'] = 'MODEL_STOPPED'
                self.stop('model_reported_no_justified_rtl_action')
            elif last['correctness'] is None and (self.dir / 'proposals' / last['proposal'] / 'candidate.v').is_file():
                self.goto('FREEZE_CANDIDATE', note='resumed unfinished proposal ' + last['proposal'])
            else:
                self.goto('BUILD_FEEDBACK', note='resumed unfinished proposal ' + last['proposal'])
            return
        why = self.stop_reason()
        if why:
            self.stop(why); return
        n = len(self.state['attempts']) + 1
        pid = f'p{n:03d}'
        pdir = self.dir / 'proposals' / pid
        inc = self.state['incumbent']
        inc_snap = self.evidence_snapshot(inc)
        base_snap = self.snap(self.man['baseline'], self.state['candidates'][self.man['baseline']]['synth_run'])
        rc = self.recent_candidate()
        packet = fb.build_packet(loop=self.man, arm=self.man['arm'], incumbent=inc_snap, baseline=base_snap,
                                 recent=[{k: a.get(k) for k in ('proposal', 'candidate', 'correctness', 'promoted')}
                                         for a in self.state['attempts'][-self.policy['feedback']['recent_attempts']:]],
                                 budget=self.remaining(), caps=self.policy['feedback'],
                                 recent_candidate=self.evidence_snapshot(rc) if rc else None)
        try:
            packet_text, dropped = fb.render(packet, self.policy['feedback']['packet_chars'])
        except GateError as e:
            self.stop('mandatory_feedback_exceeds_cap: ' + str(e)); return
        write(pdir / 'packet.json', packet)
        self.state['last_catalog'] = packet['query_catalog']
        self.state['feedback_stats'][pid] = {'packet_chars': len(packet_text), 'dropped_lines': dropped,
                                             'truncated': packet['detail_selection']['truncated'],
                                             'categories': sorted(packet['issue_counts'])}
        attempt = {'proposal': pid, 'parent': inc, 'comparison_incumbent': inc,
                   'candidate': None, 'requests': [], 'queries': [],
                   'correctness': None, 'promoted': False}
        self.state['inflight'] = attempt  # replaced wholesale on replay; persisted with every action
        extra = ''
        while True:
            if self.remaining()['model_requests'] <= 0:
                attempt['correctness'] = 'PROPOSAL_FAILED: model request budget exhausted'
                break
            k = len(attempt['requests']) + 1
            user = self.prompt(packet_text, extra)
            rdir = pdir / f'request-{k:02d}'
            rid = identity([self.man['manifest_sha256'], pid, k])  # unique across loops sharing a cost ledger
            cfg = self.policy['provider']
            nbytes, tries = len((SYSTEM + user).encode()), 1 + cfg.get('transport_retries', 0)
            ledger = self.ledger()
            aid = f'model_request:{pid}:{k}'
            if ledger and aid not in self.logged()[0]:  # replayed requests were reserved when first begun
                worst = ledger.worst_case(ledger.snapshot()['pricing'], nbytes, cfg['max_tokens'], tries)
                if worst > ledger.headroom():
                    if not attempt['requests']:  # nothing sent for this proposal: stop without spending its slot
                        self.state['inflight'] = None
                        self.stop(f'budget_exhausted:currency_ceiling (worst case {worst} > headroom {ledger.headroom()})')
                        return
                    attempt['correctness'] = 'PROPOSAL_FAILED: currency ceiling reached during queries'
                    break

            def call():
                if ledger:
                    ledger.reserve(rid, nbytes, cfg['max_tokens'], tries, self.man['manifest_sha256'])
                r = pv.call(self.transport, cfg, SYSTEM, user, rdir, rid)
                if ledger:
                    ledger.settle(rid, r['usage'], r['transport_attempts'])
                return r

            def recover(begin):  # a request may have completed: reuse its response, never resend
                if (rdir / 'response.json').is_file():
                    r = read(rdir / 'response.json')
                    if ledger and rid in ledger.snapshot()['entries']:
                        ledger.settle(rid, r['usage'], r['transport_attempts'])
                    return r
                if ledger:
                    ledger.keep_unknown(rid, 'interrupted before a response was saved')
                return {'error': 'interrupted; outcome unknown and not resent', 'content': None, 'transport_attempts': 1,
                        'usage': [], 'request_id': rid}
            resp = self.action(aid, call, recover)
            usage = [u or {} for u in resp.get('usage') or []]
            attempt['requests'].append({'request': k, 'error': resp.get('error'), 'request_id': rid,
                                        'transport_requests': resp['transport_attempts'],
                                        'prompt_chars': len(SYSTEM) + len(user),
                                        'prompt_tokens': sum(u.get('prompt_tokens') or 0 for u in usage),
                                        'completion_tokens': sum(u.get('completion_tokens') or 0 for u in usage),
                                        'response_models': resp.get('response_models'),
                                        'reasoning_content': resp.get('reasoning_content')})
            obj = pv.extract_json(resp.get('content')) if not resp.get('error') else None
            modes = ('submit', 'query', 'stop', 'alu') if self.man.get('generation') else ('submit', 'query', 'stop')
            if resp.get('error') or not obj or obj.get('action') not in modes:
                attempt['correctness'] = 'PROPOSAL_FAILED: ' + (resp.get('error') or 'reply is not a valid action object')
                break
            if obj['action'] == 'stop':
                attempt['correctness'] = attempt['outcome'] = 'MODEL_STOPPED'
                attempt['hypothesis'] = str(obj.get('reason'))[:500]
                break
            if obj['action'] == 'query':
                extra = '# Query results\n' + self.answer_queries(obj.get('queries'), attempt)
                continue
            if obj['action'] == 'alu':
                try:
                    text, meta = generation.decode(self.man['generation'], obj, self.typed_parents())
                except (GateError, KeyError, TypeError, ValueError) as e:
                    attempt['correctness'] = 'PROPOSAL_FAILED: ' + str(e)
                    break
                attempt.update(parent=meta['parent'], generation=meta)
                (pdir / 'candidate.v').write_text(text)
                attempt['hypothesis'] = str(obj.get('hypothesis'))[:1000]
                break
            if self.man.get('generation', {}).get('settings', {}).get('mode') == 'typed':
                attempt['correctness'] = 'PROPOSAL_FAILED: free-form submit is disabled in typed mode'
                break
            if not isinstance(obj.get('rtl'), str):
                attempt['correctness'] = 'PROPOSAL_FAILED: submit without rtl text'
                break
            (pdir / 'candidate.v').write_text(obj['rtl'])
            attempt['hypothesis'] = str(obj.get('hypothesis'))[:1000]
            break
        # The attempt and the next state are committed by the single save inside goto()/stop(), so a
        # crash can never leave a spent slot whose returned candidate is skipped.
        self.state['attempts'].append(attempt)  # failed and stopped proposals spend the slot too
        self.state['inflight'] = None
        if attempt['correctness'] == 'MODEL_STOPPED':
            self.stop('model_reported_no_justified_rtl_action')
        elif attempt['correctness']:
            self.goto('BUILD_FEEDBACK')
        else:
            self.goto('FREEZE_CANDIDATE')

    def answer_queries(self, queries, attempt):
        out = []
        allowed = {cid for cid, info in self.state['candidates'].items() if info.get('synth_run')}
        exposure = fb.EXPOSURE[self.man['arm']]
        for q in (queries or [])[:4]:
            r = self.remaining()
            if r['query_calls'] <= 0 or r['query_chars'] <= 0:
                out.append('query budget exhausted'); break
            limit = min(self.policy['feedback']['query_chars_per_call'], r['query_chars'])
            q = q if isinstance(q, dict) else {}
            eid, qt, cand = str(q.get('evidence_id')), q.get('query_type'), q.get('candidate', self.state['incumbent'])
            listed = next((c for c in self.state['last_catalog'] if c['query_type'] == qt and c['candidate'] == cand and
                           (c['evidence_id'] == eid or (qt == 'rtl_region' and eid.startswith('rtl')))), None)
            try:
                require(listed is not None, 'evidence id/type not in the delivered catalog')
                page = fb.query(self.root, self.evidence_snapshot(listed['candidate']), allowed, exposure, eid, qt, limit)
                text = page['text']
            except (GateError, KeyError, TypeError) as e:
                page, text = {'chars': 0, 'error': str(e)}, f'rejected: {e}'
            attempt['queries'].append({'query': q, 'chars': page['chars'], 'error': page.get('error')})
            out.append(f"## {q.get('query_type')} {q.get('evidence_id')}\n{text}")
        return '\n'.join(out) or 'no valid queries'

    def current(self):
        return self.state['attempts'][-1]

    def on_freeze_candidate(self):
        a = self.current()
        pdir = self.dir / 'proposals' / a['proposal']
        text = (pdir / 'candidate.v').read_text()
        a['rtl_sha256'] = digest(pdir / 'candidate.v')
        spec, _, _ = self.ws.verify()
        prior = next((x for x in self.state['attempts'][:-1] if x.get('normalized') == identity(normalized(text))), None)
        a['normalized'] = identity(normalized(text))
        if prior:
            a.update(correctness='DUPLICATE', duplicate_of=prior['proposal'], candidate=prior.get('candidate'))
            self.goto('BUILD_FEEDBACK', note='duplicate submission; no new evaluation, no refund'); return
        cid = self.action(f'reserve:{a["proposal"]}', lambda: {'cid': self.ws.reserve(
            a['parent'], {'op': 'loop_proposal', 'loop': self.man['loop_id'], 'proposal': a['proposal'],
                          'hypothesis': a.get('hypothesis')})}, self.recover_reserve(a['proposal']))['cid']
        a['candidate'] = cid
        self.state['candidates'][cid] = {'runs': [], 'proposal': a['proposal']}
        if a.get('generation'):
            require(a['generation']['rtl_sha256'] == a['rtl_sha256'], 'rendered candidate binding changed')
            self.state['candidates'][cid]['generation'] = a['generation']
        self.save()
        self.goto('STATIC_CHECK')

    def recover_reserve(self, pid):
        def recover(begin):
            for cid in reversed(self.ws.attempts()):
                act = self.ws.record(cid).get('action', {})
                if act.get('loop') == self.man['loop_id'] and act.get('proposal') == pid:
                    return {'cid': cid}
            return None
        return recover

    def on_static_check(self):
        a = self.current()
        spec, _, _ = self.ws.verify()
        pdir = self.dir / 'proposals' / a['proposal']
        pol, reserved = self.rtl_contract()
        why = static_check((pdir / 'candidate.v').read_text(), spec['top'], self.policy['feedback']['max_rtl_chars'],
                           pol, reserved)
        cid = a['candidate']

        def submit():
            rec = self.ws.record(cid)
            if rec['status'] != 'RESERVED':
                return {'status': rec['status'], 'error': rec.get('error')}
            if why:
                self.ws.abort(cid, 'static check: ' + why)
                return {'status': 'GENERATION_FAILED', 'error': 'static check: ' + why}
            rec = self.ws.submit(cid, pdir / 'candidate.v')
            return {'status': rec['status'], 'error': rec.get('error')}
        r = self.action(f'submit:{cid}', submit, lambda b: None)
        if r['status'] != 'SUBMITTED':
            a['correctness'] = 'STATIC_REJECTED: ' + str(r['error'])
            self.goto('BUILD_FEEDBACK'); return
        self.goto('FUNCTIONAL_CHECK')

    def on_functional_check(self):
        # One pipeline run executes the functional gate (TB + RTL SAT) and then synthesis.
        a = self.current()
        r = self.evaluate(a['candidate'], 'synthesis')
        self.state['candidates'][a['candidate']]['synth_run'] = r['run']
        self.goto('SYNTHESIZE_AND_MEASURE', run=r['run'], status=r['status'])

    def on_synthesize_and_measure(self):
        a = self.current()
        info = self.state['candidates'][a['candidate']]
        snap = self.snap(a['candidate'], info['synth_run'])
        a['correctness'] = f"functional {snap['functional']}; mapped proof {snap['mapped_proof']}" + \
                           (f"; error: {str(snap['error'])[:300]}" if snap['error'] and snap['functional'] != 'PASS' else '')
        if snap['error'] and re.search(r'sequential contract|unsupported', str(snap['error'])):
            a['unsupported'] = True
        self.goto('PHYSICAL_CHECK_IF_SCHEDULED')

    def physical(self, cid):
        r = self.evaluate(cid, 'finish')
        if self.state['candidates'][cid].get('phys_run') != r['run']:
            self.state['counters']['physical_evaluations'] += 1
            self.state['candidates'][cid]['phys_run'] = r['run']
            self.save()
        return r

    def on_physical_check_if_scheduled(self):
        a = self.current()
        p = self.policy['physical']
        snap = self.snap(a['candidate'], self.state['candidates'][a['candidate']]['synth_run'])
        eligible = snap['functional'] == 'PASS' and snap['mapped_proof'] == 'PASS' and bool(snap['synthesis']) \
            and snap['synthesis']['mapping']['status'] == 'COMPLETE'
        info = self.state['candidates'][a['candidate']]
        if 'physical_due' not in info:
            # Quota independent of synthesis ranking, so synthesis screening cannot hide wire-driven problems.
            info['physical_due'] = p['search_schedule'] == 'first_eligible' and eligible and \
                self.state['counters']['search_physical'] < p['search_quota'] and self.remaining()['physical_evaluations'] > 0
            self.state['counters']['search_physical'] += info['physical_due']
            self.save()
        if info['physical_due'] and not info.get('phys_run'):
            self.physical(a['candidate'])
        self.goto('BUILD_FEEDBACK', physical='RUN' if info['physical_due'] else 'NOT_SCHEDULED')

    def on_build_feedback(self):
        a = self.current()
        cid = a.get('candidate')
        if cid and self.state['candidates'].get(cid, {}).get('synth_run') and a['correctness'] != 'DUPLICATE':
            snap = self.snap(cid, self.state['candidates'][cid]['synth_run'])
            comparator = a.get('comparison_incumbent', a['parent'])
            inc = self.snap(comparator, self.state['candidates'][comparator]['synth_run'])
            a['decision'] = decide(self.policy, snap, inc)
            o = a['decision']['objective']
            a['synthesis_note'] = (f"synthesis: {a['decision']['outcome']}, area {o['candidate_area']} um^2 "
                                   f"(rel. improvement vs incumbent {o['area_rel_improvement_vs_incumbent']}), "
                                   f"setup ws {o['candidate_setup_ws_ns']} ns")
        self.goto('DECIDE')

    def on_decide(self):
        a = self.current()
        d = a.get('decision') or {'outcome': a['correctness'].split(':')[0], 'promote': False}
        a['outcome'] = 'DUPLICATE' if a['correctness'] == 'DUPLICATE' else d['outcome']
        if d['promote']:
            a['promoted'] = True
            self.state['incumbent'] = a['candidate']
            self.state['incumbent_history'].append({'candidate': a['candidate'], 'reason': a['proposal']})
        self.state['consecutive_no_change'] = self.state['consecutive_no_change'] + 1 if a['outcome'] in NO_CHANGE else 0
        rec = {'schema': 's2g.decision.v1', 'proposal': a['proposal'], 'candidate': a.get('candidate'),
               'parent': a['parent'], 'comparison_incumbent': a.get('comparison_incumbent', a['parent']),
               'generation': a.get('generation'), 'outcome': a['outcome'], 'promoted': a['promoted'], 'decision': a.get('decision'),
               'incumbent_after': self.state['incumbent'], 'policy_version': self.policy['policy_version'],
               'decided_at': stamp()}
        write(self.dir / 'decisions' / f"{a['proposal']}.json", rec)
        self.event('decision', proposal=a['proposal'], outcome=a['outcome'], promoted=a['promoted'])
        if a.get('unsupported'):
            self.stop('unsupported_semantics'); return
        self.goto('REQUEST_CANDIDATE')

    def on_final_confirmation(self):
        base = self.man['baseline']
        conf = self.state.setdefault('final', {'repeats': {}, 'physical': {}})
        if self.policy.get('final_selection', {}).get('kind') == 'routed_pool' and 'selection' not in conf:
            pool = {cid: self.snap(cid, info.get('phys_run') or info['synth_run'])
                    for cid, info in self.state['candidates'].items() if info.get('synth_run')}
            for cid, s in pool.items():
                s['physical_scheduled_run_missing'] = not self.state['candidates'][cid].get('phys_run')
            conf['selection'] = routed_pool_selection(self.policy, base, pool)
            conf['selection']['online_incumbent'] = self.state['incumbent']
            self.state['incumbent'] = conf['selection']['winner']
            self.save()  # pool selection is committed once and happens only after proposal generation ends
        inc = self.state['incumbent']
        for cid in dict.fromkeys([inc, base]):
            reps = conf['repeats'].setdefault(cid, [])
            while len(reps) < self.policy['final_confirmation']['repeat_synthesis']:
                if self.ws.run_count() >= self.ws.verify()[1].get('tool_run_budget', 10 ** 9):
                    conf['note'] = 'workspace tool-run budget exhausted before final confirmation'; break
                r = self.evaluate(cid, 'synthesis')
                s = self.snap(cid, r['run'])
                first = self.snap(cid, self.state['candidates'][cid]['synth_run'])
                reps.append({'run': r['run'], 'same_fingerprint': (s['synthesis'] or {}).get('structure', {}).get('fingerprint')
                             == (first['synthesis'] or {}).get('structure', {}).get('fingerprint'),
                             'area': metric_value(s, 'mapped_area')})
                self.save()
        if self.policy['physical']['final']:
            for cid in dict.fromkeys([inc, base]):
                if cid in conf['physical']:
                    continue
                if not self.state['candidates'][cid].get('phys_run'):
                    if self.remaining()['physical_evaluations'] <= 0:
                        conf['physical'][cid] = {'status': 'NOT_RUN', 'reason': 'physical budget exhausted'}; continue
                    self.physical(cid)
                s = self.snap(cid, self.state['candidates'][cid]['phys_run'])
                conf['physical'][cid] = {'run': self.state['candidates'][cid]['phys_run'],
                                         'acceptance': (s['physical'] or {}).get('acceptance', {}).get('status'),
                                         'setup_ws_ns': worst_slack((s['physical'] or {}).get('risk', {}), 'setup'),
                                         'area_um2': (s['physical'] or {}).get('risk', {}).get('area_um2'),
                                         'parasitics': (s['physical'] or {}).get('risk', {}).get('parasitics', {}).get('provenance')}
                self.save()
        label, missing = final_label(self.policy, inc, base, conf)
        conf.update(label=label, unmet_requirements=missing,
                    synthesis_reproducible=not any(m.startswith('repeat') for m in missing))
        self.goto('COMPLETE', label=label)

    # ---- reporting
    def summarize(self):
        attempts = []
        for a in self.state['attempts']:
            cid = a.get('candidate')
            costs = self.ws.costs(cid) if cid and a.get('outcome') != 'DUPLICATE' else \
                {'tool_invocations': 0, 'tool_wall_s': 0.0, 'failed_invocations': 0}
            attempts.append({k: a.get(k) for k in ('proposal', 'candidate', 'parent', 'hypothesis', 'correctness',
                                                   'comparison_incumbent', 'generation',
                                                   'outcome', 'promoted', 'rtl_sha256', 'duplicate_of')}
                            | {'decision': (a.get('decision') or {}).get('objective'), 'costs': costs,
                               'model_requests': len(a['requests']), 'queries': len(a['queries'])})
        base_costs = self.ws.costs(self.man['baseline'])
        tools = [x['costs'] for x in attempts] + [base_costs]
        out = {'schema': 's2g.loop-summary.v1', 'loop_id': self.man['loop_id'], 'arm': self.man['arm'],
               'policy_version': self.policy['policy_version'], 'state': self.state['state'],
               'stop_reason': self.state.get('stop_reason'), 'baseline': self.man['baseline'],
               'baseline_qualification': self.state.get('baseline_qualification'),
               'final_incumbent': self.state['incumbent'], 'final': self.state.get('final'),
               'attempts': attempts, 'counters': {**self.state['counters'], **self.usage(),
                                                  'candidate_submissions': len(self.state['attempts']),
                                                  'primitive_edits': sum(a.get('generation', {}).get('primitive_edits', 0)
                                                                         for a in self.state['attempts'])},
               'feedback_stats': self.state['feedback_stats'],
               'costs': {'tool_invocations': sum(t['tool_invocations'] for t in tools),
                         'tool_wall_s': round(sum(t['tool_wall_s'] for t in tools), 3),
                         'failed_tool_invocations': sum(t['failed_invocations'] for t in tools),
                         'baseline_cold_start': base_costs, 'loop_elapsed_s': self.state['elapsed_s']},
               'claim_scope': 'per-design pilot evidence; final selection=' + self.policy.get('final_selection', {}).get('kind', 'incumbent')
                              + '; physical gains require complete confirmation'}
        write(self.dir / 'summary.json', out)
        return out


def init(root, loop_id, arm, policy_path, transport, ws=None, cost_ledger=None):
    root = Path(root).resolve()
    require(arm in ARMS, f'arm must be one of {ARMS}')
    require(re.fullmatch(r'[A-Za-z0-9_.-]+', loop_id) is not None, 'unsafe loop id')
    d = root / 'loops' / loop_id
    require(not d.exists(), 'loop directory already exists; loops are never overwritten')
    ws = ws or Workspace(root)
    spec, ctx, freeze = ws.verify()
    policy = read(policy_path)
    require(policy.get('schema') == 's2g.loop-policy.v1', 'unknown loop policy schema')
    b = policy['budget']
    gen = generation.freeze(policy, spec, rtl_policy(ctx), root / 'frozen/reference.v')
    selection = policy.get('final_selection', {'kind': 'incumbent'})
    require(selection.get('kind') in ('incumbent', 'routed_pool'), 'unsupported final selection')
    if selection['kind'] == 'routed_pool':
        require(0 < selection['min_rel_area_improvement'] < 1, 'invalid routed improvement threshold')
        p = policy['physical']
        require(p.get('baseline') and p['final'] and p['search_schedule'] == 'first_eligible'
                and p['search_quota'] >= b['candidate_submissions']
                and b['physical_evaluations'] >= b['candidate_submissions'] + 1,
                'routed pool selection needs baseline and every eligible proposal scheduled physically')
    ref = digest(root / 'frozen/reference.v')
    base = next((c for c in ws.attempts() if ws.record(c).get('rtl_hash') == ref), None)
    need_slots = b['candidate_submissions'] + (0 if base else 1)
    need_runs = 1 + b['candidate_submissions'] + b['physical_evaluations'] + 2 * policy['final_confirmation']['repeat_synthesis']
    require(len(ws.attempts()) + need_slots <= ctx['candidate_budget'],
            f"workspace candidate_budget {ctx['candidate_budget']} cannot hold this loop ({need_slots} more slots)")
    require(ws.run_count() + need_runs <= ctx.get('tool_run_budget', 10 ** 9),
            f"workspace tool_run_budget {ctx.get('tool_run_budget')} cannot hold this loop ({need_runs} more runs)")
    if base is None:
        base = ws.reserve(None, {'op': 'import', 'role': 'loop_baseline'})
        ws.submit(base, root / 'frozen/reference.v')
    d.mkdir(parents=True)
    man = {'schema': SCHEMA, 'loop_id': loop_id, 'arm': arm, 'exposure': list(fb.EXPOSURE[arm]),
           'policy': policy, 'policy_sha256': identity(policy), 'workspace': str(root),
           'workspace_freeze_sha256': digest(root / 'freeze.json'), 'spec_hash': freeze['spec_hash'],
           'toolchain_id': identity(freeze['tools']), 'corner': ctx['corner'], 'platform': ctx['platform'],
           'provider': transport.identity(), 'baseline': base, 'created_at': stamp(),
           'cost_ledger': str(Path(cost_ledger).resolve()) if cost_ledger else None,
           'pricing_sha256': identity(read(cost_ledger)['pricing']) if cost_ledger else None}
    if gen:
        man['generation'] = gen
    man['manifest_sha256'] = identity(man)  # hash of every field above; checked on resume
    write(d / 'manifest.json', man)
    counters = dict.fromkeys(('physical_evaluations', 'search_physical', 'stale_evidence_rejections'), 0)
    write(d / 'state.json', {'schema': 's2g.loop-state.v1', 'state': 'REGISTER_TASK', 'incumbent': None,
                             'incumbent_history': [], 'attempts': [], 'candidates': {}, 'counters': counters,
                             'consecutive_no_change': 0, 'elapsed_s': 0.0, 'feedback_stats': {}, 'last_catalog': []})
    (d / 'events.jsonl').write_text(json.dumps({'at': stamp(), 'event': 'created', 'manifest': man['manifest_sha256']}) + '\n')
    return {'loop': str(d), 'baseline': base, 'manifest_sha256': man['manifest_sha256']}
