"""Synthesis evidence adapter: mapped area, structure, logic depth, mapped STA under the frozen
SDC, electrical checks and best-effort RTL provenance. Every metric carries unit, stage and
status; anything not actually measured is null with a reason (never zero)."""
import importlib.util
import json
import re
from collections import Counter
from pathlib import Path
from .common import *
from .netlist import Coarse, Mapped, area_unit, liberty_cells

SCHEMA = 's2g.synthesis-evidence.v1'
WIRE_MODEL = ('none: Liberty NLDM cell delays with pin-capacitance loads; the script sets no wire-load model '
              '(the library default_wire_load is not applied by this OpenSTA flow) and reads no parasitics')
SEVERITY = {'blocking': 0, 'violation': 1, 'warning': 2, 'info': 3}
TIMING_PATHS = 8


def metric(value, unit, status='VALID', reason=None, **extra):
    return {'value': value if status == 'VALID' else None, 'unit': unit, 'status': status, 'reason': reason, **extra}


def lef_sizes(ctx):
    path = Path(ctx['graph_parser_dir']) / 'lef.py'
    spec = importlib.util.spec_from_file_location('s2g_r2g_lef', path)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod.macro_sizes(str(Path(ctx['platform_dir']) / ctx['cell_lef']))


def sta_script(ctx, top, netlist, sdc, paths_json):
    plat = Path(ctx['platform_dir'])
    return '\n'.join([
        f'read_lef {tcl(plat/ctx["tech_lef"])}', f'read_lef {tcl(plat/ctx["cell_lef"])}',
        f'read_liberty {tcl(plat/ctx["liberty"])}', f'read_verilog {tcl(netlist)}', f'link_design {top}',
        f'read_sdc {tcl(sdc)}',
        'puts S2G_CHECK_SETUP_BEGIN',
        'check_setup -verbose -unconstrained_endpoints -no_clock -loops -generated_clocks',
        'puts S2G_CHECK_SETUP_END',
        f'puts "S2G_ENDPOINTS [llength [sta::endpoints]]"',
        f'report_checks -path_delay max -group_count {TIMING_PATHS} -endpoint_count 1 -fields {{input_pin slew cap fanout}} -format json > {tcl(paths_json)}',
        'puts "S2G_SETUP_WS [sta::worst_slack_cmd max]"', 'puts "S2G_HOLD_WS [sta::worst_slack_cmd min]"',
        'puts "S2G_SETUP_TNS [sta::total_negative_slack_cmd max]"',
        'report_check_types -max_slew -max_capacitance -max_fanout -violators',
        'puts "S2G_SLEW_COUNT [sta::max_slew_violation_count]"',
        'puts "S2G_CAP_COUNT [sta::max_capacitance_violation_count]"',
        'puts "S2G_FANOUT_COUNT [sta::max_fanout_violation_count]"',
        'puts S2G_STA_COMPLETE', ''])


def parse_sta(log, paths, j):
    """Pure parser. Timing is VALID only if STA completed, every endpoint is constrained and a
    finite slack was reported. Unconstrained timing is UNKNOWN, never zero or +inf."""
    out = {'status': 'ERROR', 'reason': None, 'endpoints': None, 'unconstrained_endpoints': None,
           'check_setup': [], 'paths': []}
    if j['timed_out'] or j['exit_code'] != 0 or 'S2G_STA_COMPLETE' not in log or re.search(r'^\[?ERROR', log, re.M):
        out['reason'] = 'STA timed out' if j['timed_out'] else f"STA failed (exit {j['exit_code']})"
        return out
    cs = re.search(r'S2G_CHECK_SETUP_BEGIN\n(.*?)S2G_CHECK_SETUP_END', log, re.S)
    lines = [l.strip() for l in (cs.group(1) if cs else '').splitlines() if l.strip()]
    out['check_setup'] = lines[:40]
    out['unconstrained_endpoints'] = sum(int(n) for n in re.findall(r'There (?:is|are) (\d+) unconstrained endpoint', log))

    def num(key):
        m = re.search(rf'^S2G_{key} (\S+)$', log, re.M)
        return float(m.group(1)) if m else None
    ep = num('ENDPOINTS')
    out['endpoints'] = int(ep) if ep is not None else None
    for key in ('SETUP_WS', 'HOLD_WS', 'SETUP_TNS'):
        v = num(key)
        out[key.lower()] = v if v is not None and abs(v) < 1e20 else None  # OpenSTA reports 1e30 when unconstrained
    for key in ('SLEW', 'CAP', 'FANOUT'):
        v = num(key + '_COUNT')
        out[key.lower() + '_violations'] = int(v) if v is not None else None
    try:
        out['paths'] = json.loads(paths).get('checks', []) if paths.strip() else []
    except ValueError:
        out['reason'] = 'path report unparsable'
        return out
    problems = [l for l in lines if l.startswith('Warning') or 'Error' in l]
    if out['unconstrained_endpoints'] or problems:
        out.update(status='UNKNOWN', reason='timing constraints incomplete: ' + '; '.join(problems[:3]))
    elif out['setup_ws'] is None or not out['endpoints']:
        out.update(status='UNKNOWN', reason='no finite constrained slack reported')
    else:
        out['status'] = 'VALID'
    return out


def parse_stat(stat, lib, sizes):
    mod = next(iter(stat['modules'].values()))
    by_type = mod.get('num_cells_by_type', {})
    unmapped = {t: n for t, n in by_type.items() if t.startswith('$') or t not in lib}
    unit = area_unit({c: lib[c] for c in by_type if c in lib}, sizes)
    if unmapped:
        area = metric(None, unit['unit'], 'INVALID', f'technology mapping incomplete: {unmapped}')
    elif unit['unit'] is None:
        area = metric(None, None, 'UNKNOWN', 'area unit not established: ' + unit['basis'])
    else:
        area = metric(round(mod['area'], 6), unit['unit'], basis='Yosys stat -liberty; ' + unit['basis'])
    return {'mapped_area': area, 'cell_count': metric(mod['num_cells'], 'cells'), 'cell_histogram': by_type,
            'unmapped_cell_types': unmapped}


def _path_records(paths, mapped, coarse):
    out = []
    for i, c in enumerate(paths):
        pins = [p['pin'] for p in c.get('source_path', [])]
        insts = []
        for p in pins:
            inst = p.split('/')[0] if '/' in p else None
            if inst and inst not in insts:
                insts.append(inst)
        masters = [mapped.cells.get(x, mapped.cells.get('\\' + x, {})).get('type') for x in insts]
        end = c.get('endpoint', '')
        signal = re.sub(r'\[\d+\]$', '', end) if '/' not in end else None
        if signal is None:
            cell = mapped.cells.get(end.split('/')[0]) or {}
            bits = cell.get('connections', {}).get(end.split('/')[-1], [])
            nm = mapped.netname.get(bits[0]) if bits else None
            signal = nm[0].lstrip('\\') if nm and not nm[2] else None
        rtl = coarse.cone_sources(signal) if signal else {'src': [], 'mapping_status': 'unresolved',
                                                          'confidence': 'none', 'basis': 'endpoint net is anonymous'}
        out.append({'path_id': f'path-{i + 1:03d}', 'group': c.get('path_group'), 'startpoint': c.get('startpoint'),
                    'endpoint': end, 'slack_ns': round(c['slack'] * 1e9, 6),
                    'arrival_ns': round(c.get('data_arrival_time', 0) * 1e9, 6), 'cells_on_path': len(insts),
                    'masters': masters, 'rtl': rtl})
    return out


def issues_from(ev):
    """Deterministic issue inventory (observations only; no causal claims)."""
    out = []

    def add(cat, sev, summary, refs=(), art=None, rtl=None):
        out.append({'issue_id': None, 'category': cat, 'evidence_kind': 'observation', 'severity': sev,
                    'summary': summary, 'object_refs': list(refs), 'artifact_ref': art,
                    'rtl_mapping_status': (rtl or {}).get('mapping_status', 'not_applicable'),
                    'rtl_refs': (rtl or {}).get('src', [])[:4]})
    m, t = ev['metrics'], ev['timing']
    if ev['mapping']['status'] != 'COMPLETE':
        add('mapping', 'blocking', 'technology mapping incomplete; mapped area/timing invalid: '
            + json.dumps(ev['mapping']['unmapped_cell_types']), art='stat.json')
    if ev['mapping'].get('coarse_state_cells'):
        add('mapping', 'blocking', f"{ev['mapping']['coarse_state_cells']} state/memory cells inferred in a combinational contract",
            art='coarse.json')
    if t['status'] == 'ERROR':
        add('timing', 'blocking', 'timing evidence unavailable: ' + str(t['reason']), art='sta/sta.log')
    elif t['status'] == 'UNKNOWN':
        add('constraint', 'blocking', 'timing UNKNOWN: ' + str(t['reason']), art='sta/sta.log')
    ws = m['setup_ws']
    if ws['status'] == 'VALID' and ws['value'] < 0:
        add('timing', 'violation', f"setup worst slack {ws['value']} ns (TNS {m['setup_tns']['value']} ns)", art='sta/sta.log')
    for p in ev['paths']:
        add('timing', 'info', f"path {p['startpoint']} -> {p['endpoint']}: slack {p['slack_ns']} ns, "
            f"{p['cells_on_path']} cells", [p['path_id']], 'sta/paths.json', p['rtl'])
    for k in ('slew', 'cap', 'fanout'):
        v = m[k + '_violations']
        if v['status'] == 'VALID' and v['value']:
            add('electrical', 'violation', f"{v['value']} max-{k} violations at synthesis stage ({WIRE_MODEL[:4]} wire model)",
                art='sta/sta.log')
    for w in ev['warnings']:
        add('frontend', 'warning', w, art='synthesis.log')
    out.sort(key=lambda i: (SEVERITY[i['severity']], i['category']))
    for n, i in enumerate(out, 1):
        i['issue_id'] = f'syn-{n:03d}'
    return out


def collect(root, run, spec, ctx, rtl_hash):
    """Measure one completed synthesis run. Writes synthesis_evidence.json and returns it."""
    run = Path(run); top = spec['top']
    lib = liberty_cells(Path(ctx['platform_dir']) / ctx['liberty'])
    stat = parse_stat(read(run / 'stat.json'), lib, lef_sizes(ctx))
    mapped = Mapped(read(run / 'mapped.json'), top, lib)
    coarse = Coarse(read(run / 'coarse.json'), top)
    counts = mapped.counts()
    complete = not stat['unmapped_cell_types'] and not counts['unknown_cell_types']
    sta = run / 'sta'; sta.mkdir(exist_ok=True)
    (sta / 'sta.tcl').write_text(sta_script(ctx, top, run / 'mapped.v', Path(root) / 'frozen/constraints.sdc',
                                            sta / 'paths.json'))
    j = job([ctx['tools']['openroad'], '-exit', '-no_splash', sta / 'sta.tcl'], sta, sta / 'sta.log', ctx['timeout_s'])
    write(sta / 'sta.job.json', j)
    paths_file = sta / 'paths.json'
    t = parse_sta(Path(j['log']).read_text(errors='replace'), paths_file.read_text() if paths_file.is_file() else '', j)
    if not complete:
        t.update(status='INVALID', reason='technology mapping incomplete')
    tstat = t['status']

    def tm(k):
        if tstat != 'VALID':
            return metric(None, 'ns', tstat, t['reason'])
        if t.get(k) is None:
            return metric(None, 'ns', 'UNKNOWN', 'not reported or unconstrained')
        return metric(round(t[k] * 1e9, 6), 'ns')

    def em(k):  # library max slew/cap/fanout limits do not depend on the SDC timing constraints
        if t['status'] in ('VALID', 'UNKNOWN') and t.get(k) is not None:
            return metric(t[k], 'count')
        return metric(None, 'count', 'INVALID' if not complete else 'ERROR', t['reason'])
    try:
        depth = metric(mapped.logic_depth(), 'cells',
                       definition='max combinational std cells from a startpoint (input, sequential output) to an endpoint')
    except GateError as e:
        depth = metric(None, 'cells', 'INVALID', str(e))
    warnings = sorted({re.sub(r'\s+', ' ', l).strip() for l in (run / 'synthesis.log').read_text(errors='replace').splitlines()
                       if l.startswith('Warning:')})
    ev = {'schema': SCHEMA, 'candidate': run.parent.name, 'run': run.name, 'rtl_sha256': rtl_hash,
          'stage': 'synthesis', 'corner': ctx['corner'], 'wire_model': WIRE_MODEL, 'created_at': stamp(),
          'mapping': {'status': 'COMPLETE' if complete else 'INCOMPLETE', 'unmapped_cell_types': stat['unmapped_cell_types'],
                      'unknown_cell_types': counts['unknown_cell_types'], 'coarse_state_cells': coarse.state_cells()},
          'metrics': {'mapped_area': stat['mapped_area'] if complete else metric(None, stat['mapped_area']['unit'], 'INVALID',
                                                                                 'technology mapping incomplete'),
                      'cell_count': stat['cell_count'], 'logic_depth': depth,
                      'setup_ws': tm('setup_ws'), 'setup_tns': tm('setup_tns'), 'hold_ws': tm('hold_ws'),
                      'endpoints': metric(t['endpoints'], 'count', 'VALID' if t['endpoints'] is not None else 'UNKNOWN',
                                          None if t['endpoints'] is not None else t['reason']),
                      'unconstrained_endpoints': metric(t['unconstrained_endpoints'], 'count',
                                                        'VALID' if t['unconstrained_endpoints'] is not None else 'UNKNOWN',
                                                        None if t['unconstrained_endpoints'] is not None else t['reason']),
                      **{k + '_violations': em(k + '_violations') for k in ('slew', 'cap', 'fanout')}},
          'timing': {'status': t['status'], 'reason': t['reason'], 'check_setup': t['check_setup'],
                     'sdc_sha256': digest(Path(root) / 'frozen/constraints.sdc')},
          'cell_histogram': stat['cell_histogram'], 'counts': counts,
          'structure': {'fingerprint': mapped.fingerprint() if complete else None,
                        'netlist_sha256': digest(run / 'mapped.v'),
                        'basis': 'Weisfeiler-Lehman hash of the mapped cell graph; instance/net names ignored'},
          'coarse_cells': coarse.structure(),
          'paths': _path_records(t['paths'], mapped, coarse) if complete else [],
          'warnings': warnings}
    ev['issues'] = issues_from(ev)
    ev['blocking_categories'] = sorted({i['category'] for i in ev['issues'] if i['severity'] == 'blocking'})
    write(run / 'synthesis_evidence.json', ev)
    return ev


def summary(ev):
    m = ev['metrics']
    return {'schema': SCHEMA, 'mapped_area': m['mapped_area'], 'setup_ws': m['setup_ws'], 'logic_depth': m['logic_depth'],
            'timing_status': ev['timing']['status'], 'mapping_status': ev['mapping']['status'],
            'fingerprint': ev['structure']['fingerprint'], 'blocking_categories': ev['blocking_categories']}
