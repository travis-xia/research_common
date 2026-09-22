# HypEx Workflow & Artifact Overview

本文档说明系统各节点的真实分工、产物路径与输入输出契约。供所有 Agent 只读查阅，按需调取已有产物，杜绝重复劳动。每个步骤节点只是多轮迭代中的一个轮次的一个步骤，应该有所意识，不应占据过多时间。

## 1. 流程简述 (Execution Flow)

1. **Step 0 Golden Init（仅一次）**: 三路并行（Protocol 查评测规则 + Baseline 跑基线 + Literature 文献调研）→ Synthesis 制定统一尺子与 Golden Recipe → 触发 Golden Run 跑通主干基准。
2. **Step 1 猜想与裁决 (Hypothesis & Judge)**: 并行生成 N 个猜想 (`hypo-c0/1/2`) → 规则初筛 + Judge 对决选出 Winner。
3. **Step 2 行为诊断 (Measurement)**: 若候选全否或陷入平台期触发，深挖 badcase 产出诊断报告并沉淀 `research/scripts/diagnose.py`，再回流 Step 1。
4. **Step 3 实验规划 (Plan)**: 对获胜猜想核算变量、显存与时间预算（小步长可跳过）。
5. **Step 4 工程核验与实验 (Engineer)**: Preflight 进行开工前静态代码/配置排查（只读修补）→ Run 落实代码改动、跑训练并用统一尺子评测。
6. **Step 5 知识归档 (Archive)**: 提炼因果对，写入经验卡片并追加到 `experience_bank.md`，更新状态后开启下一轮。

---

## 2. 节点职责与产物契约表 (Artifact Contract)

| 阶段 / 节点标识 | 核心功能 | 核心输入（按需读取） | 标准产物契约路径 |
| :--- | :--- | :--- | :--- |
| **Step 0-A Protocol** | 提炼官方评测启动命令、输入模板、终止符、抽取正则 | 评测源码与配置文件 | `research/protocol.md` |
| **Step 0-B Baseline** | 跑 Zero-shot 基准，记录基线分数、失败分桶与 badcase | 官方评测脚本、基座模型 | `research/baseline.md` |
| **Step 0-C Literature** | 调研候选数据集、训练范式、工程超参先验 | 联网检索、外部论文 | `research/literature.md` |
| **Step 0-Synthesis** | 冻结全 run 统一评测尺子 (Ruler)，输出 Golden Recipe 与演化路线图 | 上述 0-A/B/C 三份报告 | `research/golden_recipe.md`<br>`research/roadmap.json`<br>`research/nodes/n000-golden-synthesis/golden_synthesis.md` |
| **Step 0 Golden Run** | 执行初始化配方，跑通交付管道并全量打分，建立主干模型 | Golden Recipe、基座模型 | `research/golden_run.json`<br>`research/nodes/n000-golden-run/model/` |
| **Step 1 Hypothesis** | 针对当前瓶颈与指定靶点坐标，提出机理猜想 | 最新评测结果、`experience_bank.md` | `research/nodes/n{seq}-hyp-c{i}/hypothesis.md` |
| **Step 1 Judge** | 规则初筛 + 两两对决淘汰平庸/重复方案，选出 Winner | 所有候选 `hypothesis.md` | `research/nodes/n{seq}-judge/judge.md` |
| **Step 2 Measurement**| （瓶颈/全否触发）深挖日志归因，细化分面指标 | 最新评测日志、错误样本轨迹 | `research/nodes/n{seq}-measure/measurement.md`<br>`research/scripts/diagnose.py` |
| **Step 3 Plan** | 规划控制变量、步骤清单、显存与时间预算 | 获胜的 `hypothesis.md` | `research/nodes/n{seq}-hyp-c{win}/plan.md` |
| **Step 4 Preflight** | 启动前静态审查代码与配置，就地修补低级 bug (不占卡) | `plan.md`、修改的代码及 Diff | `research/nodes/n{seq}-hyp-c{win}/preflight.md` |
| **Step 4 Run** | 实施改动、运行训练与全量评测，记录指标与意外现象 | `plan.md`、`preflight.md` | `research/nodes/n{seq}-hyp-c{win}/result.md`<br>`research/nodes/n{seq}-hyp-c{win}/model/` |
| **Step 5 Archive** | 结构化沉淀因果对，追加更新长期知识库 | 本轮猜想、方案与实测指标 | `research/nodes/n{seq}-archive/archive_card.md`<br>`research/experience_bank.md` (追加更新) |

---

## 3. 全局共享文件 (Global State & Knowledge)

- `research/state.json`: 编排器运行状态、轮次序号、当前最佳模型路径与分数 (`best.model`, `best.score`)。
- `research/journal.md`: 全局实验流水日志，按时间顺序记录每个实验节点的得分与采纳结论。
- `research/roadmap.json`: 由 Step 0 确立的任务多阶段演化路线图与门禁参考，指导何时发起范式跃迁。
- `research/experience_bank.md`: 跨轮次沉淀的长期因果经验库（成功机制与失败教训），猜想与 Judge 节点必读。
- `research/scripts/diagnose.py`: 由 Step 2 动态生成的日志分面诊断工具脚本。
