# S2G spec→GDS research system v0.1

已实现可运行的单模块组合逻辑链路：规格审核绑定 → golden/候选导入 → TB 与 SAT 等价 → 综合与映射后等价 → OpenROAD 分阶段实现 → RCX/STA → GDS → DRC/LVS → 分阶段图、反馈、候选池和数据导出。

这是总体方案的可执行基础，不代表已经验证 LLM 优化收益，也不代表任意自然语言可以自动生成可信规格或完成工艺签核。**流程 COMPLETE 与设计 ACCEPTED 是两个状态。** 已定位并修正本地 Nangate45 LVS deck 的 PWELL 体端连接遗漏，并增加信号/体端错误负控。当前完整复验记录见 [mul8 smoke 与 debug](docs/MUL8_SMOKE_DEBUG.md)；[首轮集成结果](docs/INTEGRATION.md) 保留修复前的真实失败。

## 目录和依赖边界

所有本项目脚本和输出在 `S2G/spec2gds/`；复用的历史候选规则在 `S2G/rtl_rewrite_pilot/`。R2G 的 DEF/LEF/Liberty 解析文件已复制到 `vendor/r2g/`，附原始 MIT 许可证、commit 和逐文件哈希。**运行时不导入或执行 `Tools/agent-r2g` 的脚本。**

外部依赖仅为 EDA 工具链及工艺数据。当前显式兼容配置使用 `/opt/OpenROAD-flow-scripts/flow` 的 ORFS 工具脚本、个人目录中的 Nangate45 工艺平台和已安装的 Yosys/OpenROAD/KLayout/Icarus。ORFS 脚本在工作区内复制并固定；仪表代码只修改副本。该组合通过本项目测试，不能与历史 sky130hd 实验合并比较。版本、脚本、工具、PDK 和验证资产均有哈希。

`S2G` 根路径可改变：先修改 `configs/local-nangate45.json` 中的显式路径。`graph_parser_dir`、`typed_rules`、`typed_guard` 必须位于 S2G 内；工具和 PDK 路径可位于外部。

## 运行

CLI 退出码：`0` 达到请求阶段（finish 还要求 ACCEPTED），`1` 工具/验证失败，`2` 参数或冻结合同错误，`3` 完整执行但验收不通过。批量命令传播任一失败；仅请求 synthesis 时不要求后端验收。

核心只用 Python 标准库，建议使用 `/usr/bin/python3`，避免继承 oss-cad 的 Python 环境。

```bash
cd /data1/zhangdy/S2G/spec2gds
/usr/bin/python3 -m s2g doctor --context configs/local-nangate45.json
/usr/bin/python3 -m unittest discover -s tests -v
# root 必须不存在；实际跑两个 GDS、一个离线请求/提交和一个错误负控。
/usr/bin/python3 scripts/demo.py --root runs/my-first-demo
# 之前测试过的 mul8 原始版 + 平衡树候选，包含完整物理验收和 LVS 正负控。
/usr/bin/python3 scripts/smoke_mul8.py --root runs/my-mul8-smoke
```

示例包含 8-bit 精确加法及 ripple 改写，每个合法 RTL 做 65,536 输入对穷举，并做全二值域组合等价性证明。虚拟 IO clock 为 10 ns，非真实项目的时序承诺。`builtin_demo` 审核仅供这个例子，不能充当用户规格审核。

真实输入的基本用法：

```bash
python3 -m s2g spec-check examples/adder8/spec.json
# 审核人必须先阅读规格，填入 spec-check 给出的内容哈希；命令记录人工声明，不是电子签名。
python3 -m s2g review my-spec.json --reviewer REVIEWER --spec-hash HASH
python3 -m s2g init --root runs/project-a --spec my-spec.json \
  --reference golden.v --tb tb.v --sdc constraints.sdc --context configs/local-nangate45.json
python3 -m s2g add --root runs/project-a --rtl golden.v
python3 -m s2g run --root runs/project-a --candidate c0000
python3 -m s2g feedback --root runs/project-a --candidate c0000 --mode graph
python3 -m s2g request --root runs/project-a --candidate c0000 --mode physical
# request 保留一个预算槽，写出包含合同、RTL 和已完成反馈的 request.json。
# 外部生成器返回完整 RTL 后，提交 request 输出的 candidate id：
python3 -m s2g submit --root runs/project-a --candidate c0001 --rtl response.v
python3 -m s2g run --root runs/project-a --candidate c0001
python3 -m s2g pool --root runs/project-a
```

`spec-draft --requirements text.txt --out draft.json` 记录自然语言与待补字段，不凭空填完合同。补齐 `s2g.spec.v1` 后人工审核，独立参考实现和 TB 通过验证，再进入等价优化。模型接入采用文件 request/submit 协议；**未接入付费调用，也没有读取凭据**。thinking=false 在请求元数据中冻结，具体模型、端点、预算仍需在真实实验计划中确定。

`propose --root ROOT --action action.json [--parent ID]` 接入已有 `mul8`/`cla32` typed rules，要求规格中声明 `rewrite.design`，在固定改写区域替换结构。非法动作、生成失败、重复候选均消耗候选槽；所有动作仍必须通过完整验证。

`batch --root ROOT --candidates c0000 c0001 --repeats 2` 顺序运行重复。`candidate_budget` 和 `tool_run_budget` 分别计数；失败不退款。重复共用固定 route seed，不得称为独立随机试验。改变 seed/约束/工艺需要新 workspace。进程中断后的 RUNNING 状态不自动当作完成；保留证据，在新工作区重新执行。

## 验证和产物

- `freeze.json`：规格、工具、工艺、脚本和验证资产绑定。源变化后禁止继续旧工作区。
- `candidates/ID/record.json`：父候选、动作、源码哈希、预算槽、状态。
- `run-NNN/ledger.json`：阶段状态、工具耗时、执行代码哈希；失败原因保留。
- `backend/work/results/.../6_final.gds`：实际版图；生成它不自动代表验收成功。
- `stages/*/design.odb|design.def`：各阶段独立快照，避免后续工具覆盖早期图来源。
- `graphs/{floorplan,placement,cts,route}.json`：固定综合拓扑，分阶段坐标 mask、Liberty 面积与 LEF 尺寸、源码追踪覆盖率。
- `labels.json`、`risk.json`、`signoff.json`：与输入图分离的结果、缺失 mask、DRC/LVS 结果。
- `acceptance.json`：功能、映射后和布线后等价、setup/hold、电气检查、finish、GDS、DRC/LVS 的逐项判定。
- `evidence.json`：产物哈希；反馈和数据导出之前重新校验。

`export --root ROOT --splits family-splits.json --out dataset.json` 导出包含失败过程的清单。`family-splits.json` 格式为 `{"demo_adder8":"dev"}`。同一家族全部改写和重复在同一 split；有共同血缘但不同名称的家族须先人工归并，系统不声称自动识别代码亲缘。

可选 `pyg-export --graph PATH --out NEW_DIR --python /path/to/python` 用 **S2G 内的导出脚本** 将 JSON 转为 PyG incidence graph。缺少 torch/PyG 会报告阻塞。本机未安装这些依赖，当前已验证的是 JSON 图，不是上游 b–f / R2G2 八关系数据集兼容性。

## 明确的边界

当前后端 profile 为 Nangate45、单 corner；功能合同是单模块组合逻辑、全二值输入。顺序等价、多时钟、CDC、宏、DFT、带输入域约束的形式证明和功耗活动尚未资格验证；时序合同会被阻止执行，不用有限仿真冒充全周期证明。X/Z 不属于 SAT 等价域。不可修改合同、测试、SDC 或 floorplan 来获取改进。

图使用综合单元/端口—网络关联；坐标是单元原点，不是 pin 几何或真实拥塞。源码追踪依靠残留属性/同名信号，缺失时退到模块，不能宣称精准关键路径定位。没有训练风险预测器；反馈均标记为工具实测或结构代理。修复量是高层 repair 调用前后的实例净编辑，未完整统计 pin swap、内部回滚或路由器动作。

参见 [架构与接口](docs/ARCHITECTURE.md) 和 [总体研究方案](../docs/Physical%20Feedback%20Guided%20RTL%20Generation%20and%20Optimization_draft_v0.1.md)。

`examples/mul8/` 提供既有乘法器的桥接合同与 `balanced.json` 动作，可在独立工作区通过 `propose` 生成平衡树候选；当前完整 smoke 由 `scripts/smoke_mul8.py` 执行。
