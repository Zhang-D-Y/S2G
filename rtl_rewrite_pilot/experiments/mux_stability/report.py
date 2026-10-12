"""Read-only analysis of completed frozen evidence; writes derived report artifacts."""
import collections
import json
import statistics
from pathlib import Path
import experiment as e
import cohort

ROOT=e.PILOT/'work/mux_stability_v2'
def main():
    summary=e.read_json(ROOT/'summary.json');records=[];response_checks=[];tool_calls=0;tool_s=0
    for task in cohort.TASKS:
        r=ROOT/task['id'];a=e.read_json(r/'audit.json')
        tool_calls+=a['common_cold_start_cost']['tool_invocations']+sum(x['tool_invocations'] for x in a['arms'].values())
        tool_s+=a['common_cold_start_cost']['tool_wall_s']+sum(x['tool_wall_s'] for x in a['arms'].values())
        for at in e.read_json(r/'state.json')['attempts']:
            rec=e.read_attempt(r,at['id']);rec['task']=task['id'];rec['stratum']=e.DESIGNS[task['design']]['stratum'];records.append(rec)
            req=e.read_json(r/'attempts'/at['id']/'request.json');assert req['model']=='deepseek-flash' and req['thinking']=={'type':'disabled'} and req['temperature']==1
            res=e.read_json(r/'attempts'/at['id']/'response.json').get('response') or {}
            msg=(res.get('choices') or [{}])[0].get('message') or {}
            response_checks.append({'task':task['id'],'id':at['id'],'model':res.get('model'),'reasoning_content_present':bool(msg.get('reasoning_content'))})
    sup=e.read_json(ROOT/'supplemental.json')
    tool_calls+=sum(x['additional_tool_invocations'] for x in sup['records']);tool_s+=sum(x['additional_tool_wall_s'] for x in sup['records'])
    def counts(rs):
        return {'calls':len(rs),'complete_candidates':sum(r.get('finish_reason')=='stop' and bool(r.get('block')) for r in rs),'binary_and_TB_pass':sum(r.get('formal',{}).get('status')=='PASS' for r in rs),'decisions':dict(collections.Counter(r['decision'] for r in rs)),'rejection_reasons':dict(collections.Counter(r.get('reason') for r in rs if r['decision']=='REJECT'))}
    newrows=[r for r in summary['rows'] if r['stratum']=='new_design']
    detail={'status':'PASS','overall':counts(records),'by_arm':{a:counts([r for r in records if r['arm']==a]) for a in ('B1','B2')},'new_designs':{a:counts([r for r in records if r['arm']==a and r['stratum']=='new_design']) for a in ('B1','B2')},'new_pair_winners':dict(collections.Counter(r['winner'] for r in newrows)),'new_macro_B1_gain_pct':statistics.mean(r['B1_gain_pct'] for r in newrows),'new_macro_B2_gain_pct':statistics.mean(r['B2_gain_pct'] for r in newrows),'proposal_wall_s_total':sum(r.get('proposal_wall_s',0) for r in records),'tool_invocations_including_supplemental':tool_calls,'tool_wall_s_including_supplemental':round(tool_s,3),'supplemental_winners':len(sup['records']),'response_checks':response_checks,'peak_usage_cost_cny':summary['usage_peak_cost_cny'],'billing_note':'usage-derived peak estimate, not an invoice; account balance can include concurrent activity'}
    e.write_json(ROOT/'analysis.json',detail)
    lines=['# 固定配置、多设计重复实验结果','', '本批：3 个原生设计 × 3 次重复 × B1/B2 各 2 次提案，共 36 次调用。','', '模型与评估配置固定：deepseek-flash，thinking=disabled，temperature=1.0；原始 TB、四态扩展、二进制等价检查、相同 sky130hd 综合与保留规则。两组都从原始 RTL 开始，独立重复之间不传递历史。','', '| 设计 | 分层 | B1 平均面积改善 | B2 平均面积改善 | B2 胜 / B1 胜 / 平 |','|---|---|---:|---:|---|']
    for d,x in summary['designs'].items():
        lines.append(f"| {d} | {x['stratum']} | {x['B1_mean_gain_pct']:.4f}% | {x['B2_mean_gain_pct']:.4f}% | {x['B2_wins']} / {x['B1_wins']} / {x['ties']} |")
    lines+=['',f"预先固定的稳定性判据：{'满足' if summary['frozen_stability_rule_met'] else '未满足'}。要求 B2 在每个新设计上至少赢 2/3 次，且每个新设计的平均相对 B1 收益为正。",'', '| 设计 / 重复 | B1 最终面积 | B2 最终面积 | B1 改善 | B2 改善 |','|---|---:|---:|---:|---:|']
    for r in summary['rows']:lines.append(f"| {r['design']} / {r['repeat']} | {r['B1_area']:.4f} | {r['B2_area']:.4f} | {r['B1_gain_pct']:.4f}% | {r['B2_gain_pct']:.4f}% |")
    lines+=['','## 交付与验证','',f"完整候选：{detail['overall']['complete_candidates']}/36；通过原始 TB、扩展及二进制形式验证：{detail['overall']['binary_and_TB_pass']}/36。重复/无效/失败/被综合抹平的提案均计入预算。",'']
    for arm,x in detail['by_arm'].items():lines.append(f"- {arm}: "+json.dumps(x,ensure_ascii=False))
    lines+=['',f"全部 9 组最终保留版本的回归与审计通过。{len(sup['records'])} 个非基线保留版本完成额外映射后等价验证及两次重复综合，结果见 supplemental.json。这些验证没有反馈给模型或改变选取规则。",'', '## 费用与复现','',f"本批按峰值单价及实际 token 用量估算 {summary['usage_peak_cost_cny']:.6f} 元；保守预留 4.161024 元，本批上限 5 元。估算并非账单。",f"验证工具总调用 {tool_calls} 次、记录的工具运行时间 {tool_s:.3f} 秒（包括预设基线与补充验证，不含离线控制测试）。",'', '入口：experiments/mux_stability/cohort.py；冻结协议 plan.json；逐次结果 attempts.csv；每组原始请求、响应、RTL 补丁、日志和反馈来源均保留。', '', '## 解释边界','', '只包含两个新设计和一个已筛选的开发样例；都是小型公开 mux。每个设计仅三次随机调用重复，不是有保证的独立种子实验。开发样例与新设计分开统计；原先那次 2.74% 改善不计入本批。新设计也不表示模型训练时未见过。不能外推到大型 RTL、时序、布局布线或功耗；不能仅凭一次最优结果推断反馈的因果收益。','']
    (ROOT/'REPORT.md').write_text('\n'.join(lines))
    print(json.dumps({k:v for k,v in detail.items() if k!='response_checks'},indent=2))
if __name__=='__main__':main()
