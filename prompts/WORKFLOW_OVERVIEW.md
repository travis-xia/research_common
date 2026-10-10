# HypEx Workflow & Artifact Overview

本文档说明系统各节点的真实分工、产物路径与输入输出契约。供所有 Agent 只读查阅，按需调取已有产物，杜绝重复劳动。每个步骤节点只是多轮迭代中的一个轮次的一个步骤，应该有所意识，不应占据过多时间。

## 1. 流程简述 (Execution Flow)

1. **Step 0 Golden Init（仅一次）**: 三路并行（Protocol 查评测规则 + Baseline 跑基线 + Literature 文献调研）→ Synthesis 制定统一尺子与 Golden Recipe → 触发 Golden Run 跑通主干基准。
2. **Step 1 猜想与裁决 (Hypothesis & Judge)**: 一个 agent 一次性生成至少 N 条研究方案 (`hypotheses.md`) → Judge 直接读取批量文件，优先按最终分数潜力和可行性比较，并写出 `selected_hypothesis.md`。
3. **Step 2 行为诊断 (Measurement)**: 若候选全否或陷入平台期触发，深挖 badcase 产出诊断报告并沉淀 `research/scripts/diagnose.py`，再回流 Step 1。
4. **Step 3 端到端实验 (Experiment)**: 对获胜猜想单节点串行完成三件事——规划实验方案、开工前自查代码与配置并就地修补、占卡训练并用统一尺子全量评测。以最终分数和可交付模型为目标。
5. **Step 5 知识归档 (Archive)**: Golden Run 和每轮 Experiment 完成后都提炼因果对，写入经验卡片并追加到 `experience_bank.md`；后续 agent 通过 archive 摘要中的分数和模型路径自主判断 best。

---

## 2. 节点职责与产物契约表 (Artifact Contract)

| 阶段 / 节点标识 | 核心功能 | 核心输入（按需读取） | 标准产物契约路径 |
| :--- | :--- | :--- | :--- |
| **Step 0-A Protocol** | 提炼官方评测启动命令、输入模板、终止符、抽取正则 | 评测源码与配置文件 | `research/protocol.md` |
| **Step 0-B Baseline** | 跑 Zero-shot 基准，记录基线分数、失败分桶与 badcase | 官方评测脚本、基座模型 | `research/baseline.md` |
| **Step 0-C Literature** | 反查同类任务的最强公开方案，归纳数据、数据量、构造流程、训练与解码配置及针对性方法（均带出处） | 联网检索、外部论文 | `research/literature.md` |
| **Step 0-Synthesis** | 冻结全 run 统一评测尺子 (Ruler)，输出 Golden Recipe 与演化路线图 | 上述 0-A/B/C 三份报告 | `research/golden_recipe.md`<br>`research/roadmap.json`<br>`research/nodes/n000-golden-synthesis/golden_synthesis.md` |
| **Step 0 Golden Run** | 执行初始化配方，跑通交付管道并全量打分，建立主干模型 | Golden Recipe、基座模型 | `research/golden_run.json`<br>`research/nodes/n000-golden-run/model/`<br>`research/nodes/n001-golden-archive/archive_card.md` |
| **Step 1 Hypotheses** | 一个 agent 针对当前瓶颈一次性提出至少 N 条研究方案 | 最新评测结果、`experience_bank.md` | `research/nodes/n{seq}-hypotheses/hypotheses.md` |
| **Step 1 Judge** | 直接读取批量猜想，核验并选出 Winner，同时落盘胜出猜想 | `hypotheses.md` | `research/nodes/n{seq}-judge/judge.md`、`selected_hypothesis.md` |
| **Step 2 Measurement**| （瓶颈/全否触发）深挖日志归因，细化分面指标 | 最新评测日志、错误样本轨迹 | `research/nodes/n{seq}-measure/measurement.md`<br>`research/scripts/diagnose.py` |
| **Step 3 Experiment** | 单节点串行：规划实验方案 → 开工前自查代码/配置并就地修补 → 占卡训练与统一尺子全量评测 | Judge 写出的 `selected_hypothesis.md` | `research/nodes/n{seq}-experiment/experiment.md`<br>`research/nodes/n{seq}-experiment/model/` |
| **Step 5 Archive** | 结构化沉淀因果对，追加更新长期知识库 | Golden Run 或本轮猜想、方案与实测指标 | `research/nodes/n{seq}-archive/archive_card.md`<br>`research/experience_bank.md` (追加更新) |

---

## 3. 全局共享文件 (Global State & Knowledge)

- `research/state.json`: 编排器运行状态、轮次序号、当前最佳模型路径与分数 (`best.model`, `best.score`)。
- `research/journal.md`: 全局实验流水日志，按时间顺序记录每个实验节点的得分与采纳结论。
- `research/roadmap.json`: 由 Step 0 确立的任务多阶段演化路线图与门禁参考，指导何时发起范式跃迁。
- `research/experience_bank.md`: 跨轮次沉淀的长期因果经验库（成功机制与失败教训、score、delta、adopted、model_path），所有后续 agent 必读并据此判断当前 best。
- `research/scripts/diagnose.py`: 由 Step 2 动态生成的日志分面诊断工具脚本。
