# research —— 序列决策式研究编排器

编排器做序列决策，每个节点是一个独立的 CLI 进程，节点之间靠落盘文件通信。
产物走文件契约，不解析自然语言，所以换 CLI 不影响流程。

对 PostTrainBench 的契约与其它 agent 一致：`cwd = $JOB_TASK`，结束时在 cwd 下留下
`./final_model`。编排器不修改 harness 的任何文件。

## 目录

```
orchestrator.py        主编排器
entry.sh               两个后端共用的启动脚本
prompts/               各节点的提示词
assets/                guard.py、钩子配置、subagents、benchmark_profile
tasks/post_train/      操作空间 action_space.json（由 RESEARCH_TASK_TYPE 选择）
skills/                节点按需阅读的操作手册
```

入口在本目录之外，各是一个 `solve.sh`，只设置 `RESEARCH_CLI` 后 exec 到 `entry.sh`：

- `agents/codex_research/solve.sh` → Codex CLI，配 `gpt-*`
- `agents/claude_research/solve.sh` → Claude Code CLI，配 `claude-*`

`prompts/` 里 `step3_plan.md`、`step4_engineer1_run.md`、`step4_engineer2_preflight.md`
已不再被调用，保留仅供参考。`assets/step_policy.json` 同样不再被读取。

## 流程

```
Step0 Golden Init
   三路 specialist 并行：
     Protocol    读评测源码        → research/protocol.md
     Baseline    跑官方全量评测    → research/baseline.md
     Literature  联网调研          → research/literature.md
   然后 Synthesis 汇总，定统一尺子与完整配方
                                   → research/golden_recipe.md
                                     research/roadmap.json
                                     research/golden_init.json
   ↓
Golden Run（n000-golden-run）
   不进循环，照配方跑一次完整训练 + 官方全量评测
     成功（官方全量 Δ ≥ 0）→ 写入 best，成为主干
     失败 → 不重试，写入 research/golden_run.json，best 留空
   ↓
loop:
   [Step2 Measurement]  候选全被否决时触发，诊断后回流 Step1
   Step1 猜想 × N       并行；只下发本轮允许的层级，靶点由节点自己选
   硬规则前筛           编排器代码执行，不调 LLM
   Step1 Judge          两两对比选一个；全否则回 Step2 再重猜
   Step3 Experiment     单节点串行：规划 → 开工前自查 → 占卡训练与评测
   Step5 记录 + Archive 记账、更新 best，因果对追加进 experience_bank.md
   ↓
finalize: best → ./final_model
```

### 预算

全局预算读 harness 的 `timer.sh`，读不到退回 `NUM_HOURS`。

- Golden 阶段（Init + Run 合计）= `min(总预算 × 35%, 6h)`。
  Init 拿其中的 10/35，Run 拿扣掉 Init 实际耗时后的剩余。
  10h 预算下：Init 60 分钟、Run 约 150 分钟。
  封顶后：Init 约 103 分钟、Run 约 257 分钟，结余进循环。
- Init 内部：三路 specialist 并行，各拿 Init 窗口的 45%（10h 下各 27 分钟），
  synthesis 拿剩余、保底 55%。
- 收尾保留 8%，用于 `final_model` 交付。
- 循环内的节点没有单独的时间配额，超时等于当前剩余墙钟减去收尾保留。
  唯一的硬停是全局墙钟：到点发 SIGTERM，`finalize()` 保住 `final_model`。

### 调度

`schedule()` 只看两个量：连续无提升次数 `no_improve_streak`，和剩余预算比例。

| regime | 条件 | 允许的层级 |
|---|---|---|
| explore | 默认 | strategy + exec |
| reignite | 连续 2 次无提升，且剩余 > 25% | strategy + exec，提示词要求换路线 |
| polish | 剩余 ≤ 25% | 只许 exec |

连续 2 轮拿不到可执行猜想时，regime 追加 `+relaxed`，层级放宽到 strategy + exec。
连续 3 轮则先做一次 Measurement 再重猜，不因此收尾。

### 硬规则前筛

`prefilter_candidates()` 不调 LLM，逐条判废：

- 候选主动弃权
- 靶点不在 `action_space.json` 里，或落在冻结坐标上
- 靶点层级超出本轮允许的层级
- 同一轮内两个候选选了完全相同的靶点组合
- `cost_estimate_h` 超过剩余墙钟
- 靶点组合里的每个坐标都已试过 ≥2 次且从未被采纳

通过前筛的候选交给 Judge 两两对比，选出一个进入实验。

### 采纳

一次实验被采纳需同时满足：`status == "ok"`、模型目录真实存在、
分数相对同刻度锚点的提升 Δ ≥ `IMPROVE_EPS`（0.01）且超过采纳阈值。

采纳阈值：全量评测（`official_full`）固定为 0.01；子集评测取
`max(0.01, 标准误)`，标准误由本次评测的样本数算出。

比较对象由 `same_scale_anchor()` 按 `(eval_mode, n)` 选择：优先当前 best 的同刻度分数，
没有才退回基线。Golden Run 用官方全量打分，循环实验用统一尺子，两者刻度不同，不直接比。

### 度量

Measurement 在两类情况下触发：开局 `MEASURE_FIRST=1` 时第一轮猜想之前做一次；
候选被硬规则或 Judge 全否时做一次。

节流：全程最多 `MAX_MEASURE=4` 次；针对同一份评测结果不重复诊断。
产物 `measurement.md` 的摘要会注入下一轮猜想的提示词。

### 统一尺子

循环阶段的所有评测都用官方 `evaluate.py` 加 Golden Init 里定好的固定参数，
不自建评测器。尺子全程不换，否则历史节点的分数不可比。
全量评测太贵时才退到固定子集，子集不低于 `RULER_MIN_N=200`。

## 节点与产物

| 节点 | 提示词 | 产物 |
|---|---|---|
| Protocol | `step0_protocol.md` | `research/protocol.md` |
| Baseline | `step0_baseline.md` | `research/baseline.md` |
| Literature | `step0_literature_review.md` | `research/literature.md` |
| Synthesis | `step0_synthesis.md` | `research/golden_recipe.md`、`roadmap.json`、`golden_init.json` |
| Golden Run | `step0_golden_run.md` | `research/golden_run.json`、`nodes/n000-golden-run/model/` |
| Hypothesis | `step1_hypothesis.md` | `nodes/<nid>/hypothesis.md` |
| Judge | `step1_judge.md` | `nodes/<nid>/judge.md` |
| Measurement | `step2_measurement.md` | `nodes/<nid>/measurement.md` |
| Experiment | `step3_experiment.md` | `nodes/<nid>/experiment.md`、`model/` |
| Archive | `step5_archive.md` | `nodes/<nid>/archive_card.md`，追加 `experience_bank.md` |

每个节点目录里还有 `<label>.prompt.md`（收到的完整提示词）和
`<label>.stream.jsonl`（CLI 的完整事件流）。

`research/` 下的共享文件：

```
state.json         节点树、best 指针、轮次、平台期计数
journal.md         每个实验节点的得分与采纳结论
summary.json       收尾摘要，含 target / regime / layer 三张直方图
experience_bank.md 跨轮次的因果记录，猜想与 Judge 节点读取
guard.log          每次工具调用的放行/拦截
action_space.json  操作空间副本
harness_src/       编排器源码快照
```

节点序号单调递增（`n001-hyp-c0`、`n002-judge`…），被否掉的候选也占号，
否则它们的轨迹会被下一轮覆盖。

## 收尾与兜底

- 正常结束、收到 SIGTERM/SIGINT、或未捕获异常，都会走 `finalize()`：
  把 best 的模型目录拷成 `./final_model`。
- 没有任何节点胜过基线时，`final_model` 回退为基座模型。
- 基座是 HuggingFace id 时按本地快照目录��析，不走 `snapshot_download`。
- 网关错误（`err_kind == "api"`）退避重试，默认 3 次、间隔 90 秒；
  连续失败达到 `API_GIVEUP=6` 时抛 `GatewayDown`，用当前 best 收尾，退出码 4。
- 节点产物不合法时带反馈重试；重试耗尽后，盘上那一版带 `_contract_warning` 交给下游。
  只有盘上什么都没有才算这个节点没产出。
- Golden Init 缺任何一份产物都只记一条降级记录后继续；配方和尺子的缺项由
  `repair_recipe()` / `repair_ruler()` 补齐，补齐后的尺子一律是 `official_full`。
- `golden_run` 或 `experiment` 把训练命令丢进后台就收尾、且盘上没有产物时，
  补一次收尾，提示接着已有 checkpoint 做完评测，不重新训练。

checkpoint 只保留 best 和最近 `KEEP_CKPT=2` 个实验节点的权重。

## 环境变量

流程：

| 变量 | 默认 | 含义 |
|---|---|---|
| `RESEARCH_N_HYPO` | 3 | 每轮并行猜想数 |
| `RESEARCH_MAX_NODES` | 40 | 节点数上限 |
| `RESEARCH_JOURNAL_TAIL` | 8 | 注入提示词的最近实验记录条数 |
| `RESEARCH_TASK_TYPE` | post_train | 选择 `tasks/<type>/action_space.json` |

预算：

| 变量 | 默认 | 含义 |
|---|---|---|
| `RESEARCH_GOLDEN_RUN_FRAC` | 0.35 | Golden 阶段占预算的比例 |
| `RESEARCH_GOLDEN_CAP_H` | 6 | Golden 阶段的绝对上限（小时） |
| `RESEARCH_GOLDEN_FRAC` | 0.10 | Init 占预算的比例，实际拿信封的 `GOLDEN_FRAC/GOLDEN_RUN_FRAC` |
| `RESEARCH_GOLDEN_RUN` | 1 | 设 0 关掉 Golden Run |
| `RESEARCH_RESERVE_FRAC` | 0.08 | 收尾保留比例 |
| `RESEARCH_KEEP_CKPT` | 2 | 除 best 外保留的 checkpoint 数 |
| `RESEARCH_NODE_HARD_KILL` | 0 | 设 1 恢复到点强杀单个节点 |

采纳与度量：

| 变量 | 默认 | 含义 |
|---|---|---|
| `RESEARCH_IMPROVE_EPS` | 0.01 | 采纳所需的最小提升 |
| `RESEARCH_MEASURE_FIRST` | 1 | 首轮猜想前先做一次度量 |
| `RESEARCH_MAX_MEASURE` | 4 | 全程度量次数上限 |
| `RESEARCH_MEASURE_GAP` | 2 | 自动度量之间至少新增的实验数；`measure_anchor=-1` 时不等 |
| `RESEARCH_RULER_N` | 300 | 固定子集尺子的默认样本数 |
| `RESEARCH_RULER_MIN_N` | 200 | 子集尺子的样本数下限 |
| `RESEARCH_OFFICIAL_CHECK_LIMIT` | 150 | baseline 窗口装不下全量时的退路抽样数 |

后端与调试：

| 变量 | 默认 | 含义 |
|---|---|---|
| `RESEARCH_CLI` | claude | `claude` 或 `codex`，由 solve.sh 设置 |
| `AGENT_CONFIG` | 空 | 传给 CLI 的模型名 |
| `RESEARCH_EFFORT` | max | claude 的推理档位 |
| `RESEARCH_CODEX_EFFORT` | high | codex 的推理档位 |
| `RESEARCH_USE_AGENTS` | 1 | 设 0 关掉 claude 的 `--agents` |
| `RESEARCH_DRY_RUN` | 0 | 设 1 不调 API，用桩产物跑完整条链路 |
| `RESEARCH_DRY_GOLDEN_FAIL` | 0 | 空跑时让 Golden Run 失败 |
| `RESEARCH_API_RETRIES` | 3 | 单次调用的网关重试次数 |
| `RESEARCH_API_BACKOFF_S` | 90 | 网关重试间隔（秒） |
| `RESEARCH_API_GIVEUP` | 6 | 累计网关失败多少次后收尾 |

`RESEARCH_MIN_GROUNDED`、`RESEARCH_MIN_MECHANISTIC`、`RESEARCH_PLATEAU_K`
仍定义在 `orchestrator.py` 里，但当前没有被读取。

## 空跑

```bash
cd <一个空目录>
NUM_HOURS=1 RESEARCH_DRY_RUN=1 RESEARCH_MAX_NODES=6 RESEARCH_MEASURE_FIRST=0 \
  RESEARCH_AGENT_DIR=$PWD/agents/research_common \
  python3 -u agents/research_common/orchestrator.py
```

不调 API、不占卡，秒级走完从 Golden Init 到 `finalize` 的整条链路，
用来确认编排器自身没有运行期错误。
