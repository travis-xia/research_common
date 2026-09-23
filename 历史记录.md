# research —— 序列决策式研究 harness

核心实现：/root/ComateProjects/chats/myptbench/PostTrainBench/agents/research_common/
  主编排器：orchestrator.py
  节点提示词：prompts/
  守卫等资源：assets/
  任务操作空间：tasks/ (默认 tasks/post_train/action_space.json)
  步长策略：assets/step_policy.json
Codex 入口：/root/ComateProjects/chats/myptbench/PostTrainBench/agents/codex_research/solve.sh
Claude 入口：/root/ComateProjects/chats/myptbench/PostTrainBench/agents/claude_research/solve.sh
运行过程：
$JOB_TASK/research/
最终模型：
$JOB_TASK/final_model/
外层归档：
$OUT_DIR

nvidia-smi 显示的现有的进程是占卡进程会自动退出，不必在意

把 `/root/ComateProjects/chats/流程记录.md` 的方案实现成一条可跑的流水线：**编排器做序列决策，每个节点是一个独立的
CLI 进程，节点之间靠落盘状态和文件契约通信。**

替代的是现有 `agents/claude/solve.sh`、`agents/codex/solve.sh` 那种「一个 CLI 进程跑满 10 小时」的做法。

产物契约走文件、不解析自然语言输出，所以流程本身与具体 CLI 无关。按仓库惯例（一个 CLI 一个
agent 目录，`codex` ↔ `gpt-*`、`claude` ↔ `claude-*`）拆成两个入口，共用同一套实现：

- `agents/codex_research/solve.sh` → Codex CLI 驱动，配 `gpt-*`（与 `run.sh` 的默认组合一致）
- `agents/claude_research/solve.sh` → Claude Code CLI 驱动，配 `claude-*`
- `agents/research_common/` → 实现本体（本目录），没有 `solve.sh`，所以不会被当成一个可选 agent

## 1. 接入方式：零侵入

对 PostTrainBench 的契约与其它 agent 完全一致——`cwd = $JOB_TASK`，硬超时前在 cwd 下留下
`./final_model`。因此：

- **新增**：`agents/research_common/`、`agents/codex_research/`、`agents/claude_research/`、
  以及顶层 `run_research.sh`
- **改动**：无。`run_in_container.sh`、`evaluate.py`、`templates/`、judge、归档、其它 agent 目录一行未动

`run_in_container.sh:204` 只把 `solve.sh` 一个文件拷成 `$WORK/agent_solve.sh`，所以实现靠
`$REPO_ROOT`（`run_on_host.sh:40` 已导出）定位，并且会被复制成 `research/harness_src/`——
这样归档和反作弊 judge 都能审到流程本身。

两个后端的差异只在 `orchestrator.py` 的 `cli_cmd()` 一个函数里：

- codex：`codex [--search] exec --json --skip-git-repo-check --yolo -c model_reasoning_summary=detailed
  -c model_reasoning_effort=$RESEARCH_CODEX_EFFORT --model $AGENT_CONFIG`（与 `agents/codex/solve.sh` 对齐）
- claude：`claude --print --output-format stream-json --effort $RESEARCH_EFFORT
  --dangerously-skip-permissions --settings ... [--disallowedTools ...] [--agents ...]`

## 2. 怎么跑

```bash
# 默认：codex + gpt-5.6-terra（与 run.sh 的 AGENT=codex + gpt-5.6-terra 对齐）
bash /root/ComateProjects/chats/myptbench/run_research.sh

# 先空跑验证接线（不调 API、不占卡，秒级跑完）
RESEARCH_DRY_RUN=1 NUM_HOURS=1 STAGES=train RESEARCH_MAX_NODES=22 \
  bash /root/ComateProjects/chats/myptbench/run_research.sh

# 换 Claude Code 后端
AGENT=claude_research AGENT_CONFIG=claude-opus-4-8-thinking-sp-kiro \
  bash /root/ComateProjects/chats/myptbench/run_research.sh
```

`run_research.sh` 与 `run.sh` 逐项对齐（TASK/MODEL/AGENT/AGENT_CONFIG/NUM_HOURS/STAGES/
RUN_JUDGE/TRAJ_PORT/NUM_GPUS），额外多一组 `RESEARCH_*` 旋钮。
WORK/OUT_DIR/TRACE_ROOT 由 `run_on_host.sh` 统一派生到
`$RUNS_ROOT/<task>_<model>_<agent>_<agent-config>_<时间戳>/{work,out,traces}`，
两条路靠 AGENT 名（codex_research vs codex）区分并存；TRAJ_PORT 仍需手动错开。

## 3. 流程实现

```
Step0 Golden Init（预算 10%，从 golden 信封 35% 里出）
   ├─ 三个独立 CLI specialist 并行：
   │  ├─ Protocol：读 evaluate.py/templates/scorer → research/protocol.md
   │  │     （markdown 提醒清单，固定小节；每条结论给 文件:行号）
   │  ├─ Baseline：直接跑官方 evaluate.py 全量（--limit -1，与交付同刻度）+ 失败分桶 → baseline.md
   │  └─ Literature：有独立硬时间预算的 WebSearch → literature.{json,md}
   │       ├─ 同时调研**数据属性**与**训练范式**（只查数据源 = grid search 的数据版）
   │       ├─ 核心候选至少两类证据交叉确认，不允许只 curl 几张 HF dataset card
   │       ├─ 它与 baseline 并行、看不到真实瓶颈，所以结论必须写成条件式（"若瓶颈是 X 则…"）
   │       └─ 禁查 PostTrainBench 本身，也禁查 benchmark 的榜单/刷分/题解
   └─ 汇总 init agent（synthesis）：审计三份产物 + 做初步决策 + **写出完整配方**
      → base_plan + first_targets（首轮该打哪些坐标，要覆盖不同机制）
      → golden_recipe（可直接执行的完整训练配方，见 3.1）
   ↓
Golden Run（golden 信封 35% 的剩余，n000-golden-run）
   └─ 不进循环，先照 golden_recipe 跑一次完整训练 + **官方全量评测**建立主干
      ├─ 由 golden_run 节点一次做完：开工前自查配置 → 训练 → 记录；不走猜想、也不走 Judge
      ├─ 成功（官方全量 Δ ≥ 0）→ 写入 best，成为主干
      └─ 失败 → **不重试**，如实写 research/golden_run.json 注入后续所有节点，
         best 仍为空 → 第一轮照旧走 explore
   ↓
loop:
   [Step2 Measurement]  ← 候选全被否决时触发（有次数上限，且同一份评测结果不重复诊断）
   Step1 猜想 × N       ← 并行独立进程；每个槽位只下发本轮允许的层级，靶点由节点自己选
   硬规则前筛           ← 编排器代码执行，不调 LLM
   Step1-judge          ← 证据核对 + 两两对比；全否则回 Step2 做度量再重猜
   Step3 Experiment     ← 单节点串行：规划方案 + 开工前自查并就地修补 + 占卡训练与统一尺子评测
   Step5 记录 + Archive ← 记账、更新 best，并让归档节点把因果对追加进 experience_bank.md
   ↓
finalize: best → ./final_model
```

### 3.1 Golden Run：开局直接执行一个完整配方

对齐流程记录.md 的「黄金的流程」：**初步 SFT 建立稳定策略 → 才进入"评测 → bad case →
单机制修改 → 回滚/晋级"的循环**。原来的实现只 scale 了第一步的*调查*，第一次训练仍由
循环里的单坐标机制测试决定，主干等于被一个刚过噪声线的实验随机定下来。

支撑这个改动的证据（PostTrainBench 全站 1509 条轨迹，7 benchmark × 4 基座，均为 10h 预算）：

- **时间不是稀缺资源**。墙钟与分数的 Spearman 全局 +0.428，但加入 agent 固定效应后系数
  从 +0.182 塌到 +0.049；在 Top-5 agent 内部 rho=+0.028（p=0.63），完全没有解释力。
  边际收益在 7–9h 见顶后转负（跑满 10h 的组内分位 0.614 < 8–9h 的 0.671）。
- **交付才是稀缺的**。11.5% 的 run 交不出可评测模型、9.7% 得 0 分，超过 1/5 的算力没换来
  有效分数；`vllm serve` 起不来占了全部未评分 run 的 50.3%，跨 7/7 benchmark、4/4 基座。
- 所以开局把配方与交付管道一次跑通，比用一次昂贵的机制测试探路划算。

`golden_recipe` 契约（`validate_recipe_contract()` 校验；缺项由 `repair_recipe()` 按默认值补齐后继续跑，写 `research/golden_recipe.json`）：
`rationale` / `paradigm`（method、stages；RL 有就写进 stages，没有就不写）/ `data`（sources、
filters、answer_format、target_n）/ `hyperparams` / `generation_config` / `checkpoint_selection` /
`steps[]`（含 cmd 与 est_min）/ `budget` / `success_criterion` / `abort_criterion` / `fallback`。

**为什么不复用 Step3**：Step3 的产物字段（steps、budget、success/abort 判据）直接合并进
recipe，由 synthesis 一次写出——它手里有三个 specialist 的全部产物，比只看一个猜想的
Step3 信息更全。代价是 synthesis 不持卡，`est_min` 只能是先验估算，所以 Golden Run 的
超时不夹到这个估值上，而是给 `min(信封剩余（总预算×35% − Init 已耗）, 剩余 − 收尾保留 − 0.3h)`。

硬校验会挡下的情况（每条都对应一个跨任务先验）：缺字段、`epochs > 3`（3 epoch 过拟合在
4 个 benchmark 上有记录）、`generation_config` 的四个采样键不全（推理引擎不读 `do_sample`，
缺 `temperature` 就退回 1.0，实测能吃掉 7~80 多个百分点）、`method` 非 `full_sft` 又不给
`lora_justification`（LoRA/QLoRA 在 7/7 上都是低分组标记，失败在输出协议层）、
`budget.eval_mode` 不是 `official_full`。

**刻度问题**：Golden Run 用官方全量打分（与 Golden Init 基线同刻度），而循环实验用统一
尺子（可能是固定子集）。所以 `same_scale_anchor()` 按 `(eval_mode, n)` 挑同刻度的比较
对象：best 与本次同刻度就用 `best.score`；否则用 Step1 给主干打的尺子锚点
`best.ruler_metrics`；都没有才退到 `baseline.official` / `baseline.ruler_metrics`。
对应地，Step1 的尺子锚点规则是：**有主干时评测主干，没有主干时才评测基座**。

### 3.2 Step1 Measurement（指标改进）

**不再自建 dev 集/评测器**（2026-09-07 用户决策）：官方 `evaluate.py` 自带
`--limit` / `--max-tokens` / `--max-connections` / `--gpu-memory-utilization`，统一尺子
就是官方脚本加固定参数；Step1 只建两样东西——`research/scripts/diagnose.py`
（纯 CPU 的官方日志分面分析器）和尺子锚点评测（见 3.6）。

只回答一件事：**现象被怎样描述才看得清**。它**不**决定要不要走大步——那属于 Step2 的退火，
由 `schedule()` 负责。两件事混在一个节点里会让职责糊掉。

提示词把分面收敛到四类（对齐流程记录.md），要求按判断挑 2–4 个而不是全做：

- **子领域知识**：数学切代数/几何/数论；GPQA 切物/化/生；代码切数据结构/字符串/数学
- **能力维度**：计算精度、逻辑规划、概念理解、格式遵循
- **过程检查**：正确答案已出现在推理里但最终答案写错 / 自我修正后改错 / 复读 / 提前放弃 /
  工具调用率与参数错误率
- **条件正确率**：长 vs 短回答、题干长短、推理步数、是否截断、是否命中某格式模板

触发判据（`measurement_reason()`，编排器侧规则，不交给 LLM 自己想）：

1. `measure_pending` —— judge 把问题归因到度量侧（**不受间隔限制，立刻回 Step1**）
2. 上一个实验 `verdict == inconclusive`
3. 上一个实验 `|Δ| < 采纳阈值`（落在噪声带内）
4. 连续 `PLATEAU_K` 个节点无提升
5. `RESEARCH_MEASURE_FIRST=1` 时，第一轮猜想之前先做一次（Golden Init 的分桶是粗桶，
   不足以支撑"三路各守一个坐标"）

节流：全程最多 `MAX_MEASURE=4` 次；除第 1 条外，自动触发还要求距上次度量又跑了
`MEASURE_GAP=2` 个实验。没有这个闸，平台期里每一轮都会重跑尺子评测把预算吃光。
产物里的 `suggested_targets` 会回喂给坐标分配。

### 3.3 调度：三态，不再按步长分档

步长分档（large/medium/small）与 `recipe_stable()` 已在 2026-09-22 删除，`schedule()`
现在只按剩余预算和连续无提升轮数切三态，不再下发步长、也不再逐条校验步长契约：

- `explore`（默认）→ strategy + exec 全开，不限模块数
- `reignite`（连续 `no_improve_streak >= 2` 且剩余 > 25%）→ 同样全开，但提示词要求跳出局部调优
- `polish`（剩余 ≤ 25%）→ 只许 exec 层

`assets/step_policy.json` 仍保留这三态的说明文字，但编排器不再读取它，提示词里也没有
`{{STEP_POLICY}}` 占位符，它目前不参与任何决策。

坐标层面的约束不在 `schedule()` 里做，而在 `prefilter_candidates()` 的硬规则里：
同一轮内完全相同的靶点组合直接判废；跨轮上，一个靶点组合里的每个坐标都已试过 ≥2 次
且从未被采纳时，该组合也判废。所以"换个说法再做一次"会被挡掉，但被采纳过的坐标不受影响，
可以在它之上继续叠加。

**预算没有中间时间配额**（2026-09-07 用户决策）：`cost_cap_h = 全部剩余时间`（收尾保留
已扣掉）。原来按步长上限和剩余预算比例双重封顶会让"9 分钟节点预算"这种事真实发生
（第 4 次 run 的 n081 因它被迫子集评测、n032 因按比例的 per-update 耗时中止线被 abort）；
现在唯一的硬边界是全局墙钟与收尾保留，**中间每一步跑多久由节点自己决定**。

### 3.4 机制锚点自主归因与范式推进（废除死板退火）

科研假说必须是**现象驱动（Phenomenon-driven）**的。编排器不再硬性指定具体坐标与观察视角，而是由模型参考机制操作空间（`action_space.json`）与前序现象自主提出假说。

- **机制锚点自主归因**：Step1 读取最新评测样本、Bad Case 和诊断报告，从机制操作空间中自主诊断瓶颈并选定干预靶点列表 `targets: [{domain, module}, ...]`。
- **废除模块数量硬退火，支持范式跃迁（Multi-stage Progression）**：
  - 不再按 `3 -> 2 -> 1` 硬性卡死靶点模块个数，避免将引入 RL 等多模块协同做法制度性扼杀。
  - **允许基于 SFT best 开启 RL/GRPO**：当模型格式与停机稳定但推理泛化达到天花板时，允许并在因果闭环的前提下鼓励基于当前 best checkpoint 引入强化学习或多阶段训练。
  - **因果耦合要求**：允许多模块协同，但多模块必须具备明确的因果关联（如改变训练范式同时调整奖励/采样机制）。
- **极简调度三态**：
  - **常态探索（explore）**：完全不控制步伐大小与模块数量，只要因果闭环即可自由探索、在 best 上持续叠加，鼓励多阶段范式跃迁（如基于 SFT 接 RL）；
  - **平台期重新点火（reignite）**：连续 2 次无提升时提示跳出局部调优、尝试全新路线或跃迁到 RL；
    坐标约束由硬规则执行——试过 ≥2 次且从未采纳的坐标会被判废，被采纳过的坐标不剔除，可继续叠加；
  - **收尾保护（polish）**：剩余预算尾声（≤25%）提示禁止发起耗时巨大的长周期重训，聚焦于快速收敛的稳妥参数调优。
- **多样性与防撞车**：由编排器硬规则在同一轮并行候选间禁止完全相同的靶点组合，由 Step1 自主发掘互补的机理方向。

### 3.5 Judge：硬规则 + 两两对比

**第一层（编排器代码，不调 LLM）** `prefilter_candidates()`：

- 主动弃权 / 靶点非法或冻结 / 层级越权 / 本轮候选靶点组合完全相同
- **墙钟装不下**：`cost_estimate_h` 超过剩余墙钟
- **跨轮重复**：靶点组合里的每个坐标都已试 ≥2 次且从未被采纳

 **第二层（LLM）** `step1_judge.md`：只做两件事——① 打开 `last_run.jsonl` 逐个核对引用的
sample_id 与统计数字，给 `grounded/mechanistic/novelty`（1-5）；② 对存活候选**两两对比**，
每对只回答"哪一个的失败能排除更大一类做法"。不按"哪个更可能涨分"选，因为 LLM 预测不准分数。

放行门槛：`grounded >= MIN_GROUNDED(4)` 且 `mechanistic >= MIN_MECHANISTIC(3)`。

**回流**：judge 全否时必须填 `insufficient_reason`：

- `measurement` → 编排器置 `measure_pending`，**下一轮先回 Step1 把度量做细**
- `quality` → 带着 `repair_hint` 重开一轮猜想

### 3.6 统一尺子（ruler）：全程一把尺子，能不切就不切

**不再自建 dev 集/评测器**（2026-09-07 用户决策）。官方 `evaluate.py` 自带
`--limit` / `--max-tokens` / `--max-connections` / `--gpu-memory-utilization`，所以
循环阶段的尺子就是**官方脚本本身加固定参数**——自建实现复现不了官方语义
（`max_tokens` 与 `generation_config` 的取小行为、采样兜底），曾直接烧掉过一次完整 run。

尺子在 **Golden Init 一次性定稿**（synthesis 输出 `ruler`，`validate_ruler()` 校验），
全程不得更换：

- **全量优先**。判据是 evidence-based：baseline 刚跑过官方全量，其实测耗时
  × 预计十次量级的评测次数，就是"全量装不装得起"的答案；
- 全量装不起才退**固定子集**：`--limit N`，**N ≥ 200**（子集太小噪声淹没信号：
  n=150 时 se≈0.04、n=300 时 se≈0.029，采纳阈值挂在这个 se 上）；
- 尺子的语义参数（`--limit`、`--max-tokens`）和速度参数（`--max-connections`、
  `--gpu-memory-utilization`，以本脚本真实存在的 flag 为准）都钉进 `ruler.cmd`，
  任何节点不得临场改。取值来自 protocol.md 的建议 + baseline 实测命令。
- 逐样本结果就在官方日志 `logs/`（取最新一份做分析）；分面指标用
  `research/scripts/diagnose.py`（Step1 构建）在日志上算，纯 CPU，不跑模型。
- **Golden Run 例外**：主干用官方 `--limit -1` 全量打分，与 Golden Init 基线同刻度。
- 每份 metrics 必须带 `n` 与 `eval_mode`（`official_full` | `official_subset`），
  `official_subset` 的 `n` 必须等于尺子的 n。

锚点：Step1 首次用尺子评锚点模型（有主干时是主干、否则基座）并声明
`rerun.is_baseline`，编排器登记为 `best.ruler_metrics` / `baseline.ruler_metrics`；
`same_scale_anchor()` 按 `(eval_mode, n)` 挑同刻度比较对象（step5_record）。

这段纪律写在 `eval_policy_text()`（Golden Init 后动态读取 `golden_init.json` 的
`ruler` 字段），统一注入所有节点，避免各节点各自发明 `--limit`。

### 3.7 采纳阈值：全量定死、子集挂噪声

全量评测（`official_full`）采纳阈值一律定死为`IMPROVE_EPS`（0.01）。
子集评测（`official_subset`）本身更抖，仍挂到噪声上`max(IMPROVE_EPS, se)`，`se = sqrt(0.25/n)`（n=300时se≈0.029）。

旧版固定 `IMPROVE_EPS=0.005` 远小于采样噪声，任何"提升"里都必然混着噪声。
阈值、`eval_mode`、`n` 都会写进 journal 和 state，后续能审。

### 3.8 Step3：规划、开工前自查、训练评测合并为一个节点

2026-09-22 起，原来的 Step3 规划、Engineer_2 开工前审查、Engineer_1 训练评测三个节点
合并成一个 `step3_experiment.md`，由单个进程按顺序做完，产物是 `experiment.md`
而不是 `plan.json` / `preflight.json` / `result.json`。合并的原因是三者共享同一份上下文，
拆开只是多付几轮 prompt 开销，并没有换来更干净的归因。

节点内部仍按这个顺序做事，前一步没做完不许跳到后一步：

- **规划**：写清控制变量、步骤、显存与时间预算。
- **开工前自查（不占卡）**：在真正启动训练前，对照猜想核对已写好的脚本与配置。
  这一步只排工程问题，不做方法论评判——查代码语法与逻辑、超参是否与方案一致（防笔误）、
  prompt 模板与答案标记是否逐字对齐协议、`eos_token_id` 是否覆盖协议里的全部终止符、
  采样四键是否齐全。发现问题直接改盘上的文件，不设"缺什么就停跑"的闸门。
- **训练与评测**：占卡跑训练，模型存完立刻用统一尺子打分。有 `contamination_check.py`
  时必须用它自查训练数据。不许擅自扩大方案范围，否则结果不可归因。

编排器侧只认 `experiment.md` frontmatter 里折出来的 `metrics_dev`，采纳门槛是
`status == "ok"` + 模型目录真的存在 + Δ 超过同刻度采纳阈值。

开工前自查要过的清单（按历史轨迹里"分数损失 × 频率"排序）：

1. **交付管道可用**（头号杀手，占全部未评分 run 的 50.3%）：目录完整性（权重/config/
   tokenizer 齐全、`safetensors.index.json` 列出的分片全部存在、**不许残留
   `adapter_config.json`**、gemma 系还要 `preprocessor_config.json`），以及能真的被
   `vllm serve` / 官方 `evaluate.py` 加载。
   踩过的坑：`HFValidationError: Repo id must be...` 不是路径写错，是缺 `config.json`。
2. **停机符**：`eos_token_id` 要覆盖 `protocol.md`「终止与生成上限」列出的全部终止符。基座自带的通常只有
   一个值（Qwen3-Base 只有 `151643`），而模板每轮结束发 `151645`。
   停机相关问题命中 332/543 条有正文的 digest，7/7 benchmark、4/4 基座，**分布最均匀的坑**。
   各基座的集合不同（Qwen3 `[151645,151643]`、gemma3 `[1,106]`、SmolLM3 `[128012,128001]`），
   所以从 protocol 读、不写死。
3. **解码参数显式且可复现**：四个采样键必须全部存在（缺一个就会被引擎按默认值兜底）。
   **不再断言"必须 temperature=0"**——短答案任务上贪心最优，但长 CoT 任务上贪心会陷入
   推理死循环（有轨迹记录 0/30 能闭合 `</think>`，改成 0.6 后终止率 ~13%→~63%），
   超长生成下贪心还在 offline/server 之间不可复现。判据是"四键齐全 + 与协议一致"。
4. **生成长度上限**：模型 `generation_config.max_new_tokens` 与 harness 请求值**取小者**生效。
5. 训练/评测一致性；6. `chat_template` 未被改坏。

**退火与重新点火**的完整逻辑见 3.3。

## 4. 硬约束怎么落地的

分两层，从最硬到最软：

**第一层（仅 claude 后端）：PreToolUse 钩子**（`assets/guard.py` → `research/guard.py`）：

- 写 `evaluate.py` / `templates/` / 编排器账本（含 `research/step_policy.json`）→ 拦（bench 规则）
- `research/protocol.md` 只在 Golden Init 的 protocol specialist / Measurement 阶段可写，
  之后是既定事实
- Bash 写保护判定前先摘掉 `2>&1` / `1>&2` 这类 fd 复用（2026-09-08 用户决策：曾把
  `bash timer.sh 2>&1 | head` 误判成写 timer.sh，Opus 5 live 的 13 条 deny 全是此形态；
  摘掉后 `echo x > evaluate.py` 仍拦、重定向+提到保护名仍拦，10 例单测通过）
- `rm` 研究状态 / 模型目录 → 拦
- 每次判定追加到 `research/guard.log`，作为反作弊 judge 的审计证据
- 守卫自身 fail-open：它出错就放行，不做流水线的单点故障

不设 GPU 租约（2026-09-11 用户决策）：曾按阶段给 `CUDA_VISIBLE_DEVICES` 置空、并由钩子拦截
阶段外的占卡命令（`evaluate/train/sft/grpo/dpo/torchrun/deepspeed/vllm`），结果把 preflight
的包版本检查（`python -c "import torch..."` 这类纯查版本、不真占卡的命令）一起误杀，还让
agent 反复绕钩子浪费预算。已删除两层强制：`child_env()` 不再摘卡，guard 不再按阶段拦占卡命令。
调度纪律只靠提示词约束 + 编排器侧校验。

codex 0.135 也有 PreToolUse 钩子（二进制里能看到 `Command blocked by PreToolUse hook`、
`PreToolUseHookSpecificOutputWire`），但它走的是 plugin `hooks.json` + hook trust 那一套，
**本版本没有为 codex 后端接钩子**。所以用 codex 后端时，文件级保护只剩提示词约束 +
编排器侧校验。

**第三层：编排器侧校验。** 见 3.5 的候选硬规则前筛。注意校验的作用是**给节点反馈 + 记降级项**，不是闸门：2026-09-10 起编排器不因为缺产物/缺字段停 run（见 6.1）。
`Harness.Orchestration` 与 `Others.EvalProtocol` 是冻结坐标——被评估对象不许改评估流程。

## 5. 产物

```
research/
  protocol.md            评测协议提醒清单（固定小节的 markdown；后续所有节点读它，不再重复读源码）
  baseline.md            官方全量基线与 bad-case 分桶（带 frontmatter）
  literature.md          带 query/URL/evidence ledger 的人类可读调研报告
  golden_init.json       Golden Init 编排器内部账本（ruler、baseline、组件状态、降级记录）
  golden_recipe.md       待执行的完整配方工单（Markdown，Golden Run 直接读它）
  golden_run.json        主干节点结果：配方、官方全量分数、Δ、实际发生、给后续轮次的先验
  state.json             节点树、best 指针、平台期计数、度量账本、序号
  journal.md             人类可读的猜想-实验对日志（跨节点 memory）
  summary.json           收尾摘要：target / regime / layer 三张直方图
  guard.log              每一次工具调用的放行/拦截审计
  action_space.json      操作空间（机器可读）
  step_policy.json       三态调度的说明文字（编排器不再读取，不参与决策）
  settings.json          钩子配置（渲染出绝对路径）
  harness_src/           编排器源码快照
  scripts/               diagnose.py：官方日志的分面分析器（Step1 产出，纯 CPU）
  nodes/<nid>/
    *.prompt.md          该节点收到的完整 prompt
    *.stream.jsonl       CLI 的完整事件流（轨迹采集的原始数据）
    measurement.md / hypothesis.md / rejected.json / judge.md /
    experiment.md / archive_card.md
    model/               该节点的 checkpoint（会被 GC）
```

节点序号是单调的（`n001-measure`、`n002-hyp-c0`…），被否掉的候选也占号，否则它们的轨迹会被
下一轮覆盖。

注入给节点的上下文是**分层**的：`protocol.md` 原文 + `golden_init.json` 汇总 +
`golden_run.json`（主干是怎么来的、配方哪部分已验证过、失败在哪一环）+
`action_space.json` + `step_policy.json` + `best/baseline` 摘要 +
最近 `JOURNAL_TAIL=8` 个实验节点的压缩记录（完整日志让它自己去读文件）。
这样后期节点的输入 token 不会随日志线性膨胀。

注入格式上另有三处瘦身（2026-09-09 用户决策、2026-09-14 补第三处，实测后期节点 prompt 省 ~21%+）：
所有注入 JSON 走**紧凑序列化**（`inject_json`，无缩进、无多余空格；字符串字段
——尺子命令、配方步骤——逐字保留），落盘产物仍是 `write_json` 的 indent=2 人可读格式；
`golden_run.recipe` 与 `golden_init.golden_recipe` 逐字相同时**注入前去重**
（留一行指路注释，原文在 `research/golden_run.json`），两者分叉时才全量注入；
`golden_init.golden_recipe` 与 `golden_init.data_research` 只对消费节点有用
（前者 Golden Run 已走 `{{HYPOTHESIS}}` 拿到、后者 synthesis 直接读盘上 `literature.json`），
所以对其它节点也瘦身为一行带路径的指路注释，需要时按路径打开阅读。

另设 `agents/research_common/skills/` 作为**本机操作手册**（2026-09-09 决策）：每篇
= 触发条件 + 前因后果（附实测数字）+ 可直接跑的脚本；**用得到该手册的那几个节点** prompt 里
只留**一行带触发条件的指针**（"要干 X 前必读 Y"），细节让节点按需去读文件。首篇
`skills/hf-download.md` + 包装脚本 `hf-dl.sh`：本机下 HuggingFace 大数据集必须走
8188 代理 + aria2c 多连接（单连接 ~1.4MB/s 且 10MiB 被掐，多连接实测 100-200MB/s，
差两个数量级）。

## 6. 预算、资源与兜底

- 全局预算读 harness 的 `timer.sh`，时间纪律**只管两头**（2026-09-07 用户决策）：
  golden 阶段（Init+Run）合计 35%（2026-09-22 用户纠偏：Init 从信封里出，不叠加）——
  Init 10%（10h 下 60 分钟，2026-09-22 用户决策从 13.33% 降档）、Run 拿信封剩余
  （10h 下 ≈150 分钟，主干要跑足）、收尾保留 8%
  （final_model 交付顺利）；**中间步骤没有
  按步长/按比例的硬配额**——每个节点的 CLI 超时=当前活墙钟（剩余−收尾保留），
  Bash 单命令超时（`BASH_MAX_TIMEOUT_MS`）同墙钟，240 分钟的硬上限已删。挂死的兜底
  是 harness 的 `NUM_HOURS` 定时器 + SIGTERM→`finalize`，final_model 不会丢
- Golden Init 三路并行；内部窗口**直接按 10h 总预算定值**（2026-09-09 用户决策，
  不再 20%/50%/2/3 层层比例推导，`RESEARCH_LIT_MIN`/`RESEARCH_BASELINE_FRAC`
  两个旋钮取消）：protocol / baseline / literature 各拿 Init 窗口的 45% 硬窗口
  （10h/60 分钟下各 27 分钟，并行段墙钟 27），synthesis 拿 deadline 剩余、保底 33 分钟；
  总预算偏离 10h 时按 45/55 同比缩放。
  literature 仍把「提问/广搜/深查/综合」按 15%/35%/30%/20% 分配。编排器对整段
  Web 调研而非单次 CLI attempt 计时，重试也不能突破这段预算
- 单卡串行：实验按节点独占一张卡；非实验阶段不强制摘卡（2026-09-11 用户决策），
  占卡与否由提示词纪律约束
- checkpoint GC：只保留 best + 最近 `KEEP_CKPT=2` 个节点的权重（4B bf16 一份约 8GB）
- **SIGTERM/SIGINT** → 立刻 `finalize()`，退出码 143（harness 超时杀进程时不会丢 final_model）
- **崩溃** → `except` 里也调 `finalize()`
- **网关 5xx** → `run_cli_resilient()` 区分「网关挂了」和「节点没做好」：前者退避重试
  （默认 3 次 × 90s），连续 6 次失败抛 `GatewayDown` → 收尾退出 4，不空转烧时间。
  codex 侧额外把 `turn.failed` / `error` 事件当作网关问题
- **连续 2 轮拿不到可执行猜想** → `+relaxed`：放宽层级限制（否则会空转烧 API）；
  连续 3 轮 → 强制回 Step1 先把度量做细，**不收尾**（2026-09-10 用户决策）
- **Golden Init 允许 `status: partial`**（protocol 与 baseline 都是）。提示词明确要求
  「读不到就标 unknown，不要猜」「没跑完就写 partial」；如果 partial 直接判整个 run 失败，
  等于逼节点谎报 ok，而且会让一个 10 小时的 run 在 1.8 小时后就交出基座模型。
  partial 会写进 `golden_init.component_status`，synthesis 节点仍会独立审计一遍
- **编排器不因为"缺东西"停 run**（2026-09-10 用户决策：orchestrator 是辅助不是监管）。
  唯一能终止 run 的是**时间**（全局墙钟 / 收尾保留 / `MAX_NODES`）与**工具不可用**
  （`GatewayDown`）。具体地：
  - 节点契约不合法时先带反馈重试一次；重试用完仍不合法，**盘上那一版原样交给下游**
    （带 `_contract_warning`），只有盘上什么都没有才算这个节点没产出（`run_node` 的
    salvage 分支）
  - Golden Init 缺 protocol / baseline / literature / synthesis 中的任何一个，都只记一条
    `golden_init.degradations` 继续走；配方与尺子缺字段由 `repair_recipe()` /
    `repair_ruler()` 按 `skills/golden-recipe.md` 的默认值补齐（ruler 优先沿用 baseline
    实测过的完整命令），Golden Run 照补齐后的版本跑
  - `degradations` 会随 `GOLDEN_INIT` 注入后续每个节点——开局缺了什么，它们自己心里有数
  - 唯一没让的是**尺子刻度**：补出来的 ruler 一律 `official_full`，否则历史节点不可比
- **没有任何节点胜过基线** → `final_model` 回退为基座模型（分数即基线），
  而不是让 `run_in_container.sh:277` 因缺 `final_model` 直接 exit 2 + failed 归档

## 7. 可调旋钮（都是环境变量，便于做消融）

流程宽度与门槛：
`RESEARCH_N_HYPO=3` 并行槽位数｜`RESEARCH_MIN_GROUNDED=4`｜`RESEARCH_MIN_MECHANISTIC=3`｜
`RESEARCH_PLATEAU_K=2` 平台期判据｜`RESEARCH_MAX_NODES=40`｜`RESEARCH_JOURNAL_TAIL=8`

度量与采纳：
`RESEARCH_MEASURE_FIRST=1` 首轮先做一次度量｜`RESEARCH_MAX_MEASURE=4`｜
`RESEARCH_MEASURE_GAP=2`｜`RESEARCH_IMPROVE_EPS=0.01` 采纳阈值下限（实际阈值挂噪声）

统一尺子：
`RESEARCH_RULER_N=300`（固定子集的默认 n）｜`RESEARCH_RULER_MIN_N=200`（子集下限，
防止噪声淹没信号）｜`RESEARCH_OFFICIAL_CHECK_LIMIT=150`（仅 Golden Init baseline 的退路）

预算与资源：
`RESEARCH_GOLDEN_FRAC=0.10`（10h 下 Golden Init 60 分钟）｜`RESEARCH_GOLDEN_RUN_FRAC=0.35`（Init+Run 合计的信封，Run 拿剩余）｜`RESEARCH_GOLDEN_RUN=1`
（设 0 可关掉 Golden Run，退回「第一次训练由循环决定」的旧行为，便于做消融）｜
`RESEARCH_RESERVE_FRAC=0.08`｜`RESEARCH_KEEP_CKPT=2`

后端与调试：
`RESEARCH_CODEX_EFFORT=high`（codex 档位）｜`RESEARCH_EFFORT=max`（claude 档位）｜
`RESEARCH_DRY_RUN=1` 空跑｜`RESEARCH_DRY_GOLDEN_FAIL=1` 空跑时让 Golden Run 失败，
用来验证主干没建起来时第一轮照旧走 explore 的兜底路径｜`RESEARCH_USE_AGENTS=0` 关掉 claude 后端的 `--agents`｜
`RESEARCH_API_RETRIES=3`｜`RESEARCH_API_BACKOFF_S=90`｜`RESEARCH_API_GIVEUP=6`

（`RESEARCH_PASS_TOTAL` 已移除：judge 不再用六维总分放行，改成硬规则 + 两两对比，
门槛拆成 `MIN_GROUNDED` / `MIN_MECHANISTIC`。）

推理档位不再像 `codexhigh` / `codex_xhigh` 那样靠改 `config.toml`（那两个 agent 写的是
`/home/ben/.codex/config.toml`，同事镜像的遗留路径，本机 `$HOME=/root` 时无效），
统一用 `-c model_reasoning_effort=` 传，所以不需要为每个档位单独建 agent 目录。

## 8. 已验证 / 未验证

### 8.1 本次改造（对齐新版 流程记录.md）已验证的部分

全部是**空跑 + 单测**级别，没有真实 GPU 回合：

- `python3 -m py_compile`（orchestrator / guard / loop_probe）、四个 JSON 资产合法、
  `bash -n`（run_research.sh / entry.sh / 两个 solve.sh）
- **Golden Run 两条路径都空跑跑通**（10h 预算、`MAX_NODES=40`）：
  - 成功路径：`Golden Init → Golden Run（官方全量 0.55 vs 基线 0.30，Δ0.25）→ 写入 best
    → Round 1 = climb_wide`；Step1 正确把**主干**（而不是基座）在统一尺子上的分数登记成
    `best.ruler_metrics=0.53（official_subset n=300）`，后续实验的 `delta_vs` 显示
    `best n000-golden-run 的尺子锚点`（**跨刻度比较已被 `same_scale_anchor()` 挡住**）
  - 失败路径（`RESEARCH_DRY_GOLDEN_FAIL=1`）：不重试、写出
    `golden_run.json{adopted:false, what_actually_happened, surprises,
    lesson_for_next_rounds}`，best 仍为空 → `Round 1 = bootstrap`，
    Step1 退回登记基座的尺子锚点
  - `golden_recipe` 校验单测：合法配方通过；`lora` 无理由 / `epochs=5` /
    缺 `temperature` / `eval_mode` 非 `official_full` / 开局上 RL 无前置说明 /
    缺 `steps` 六种情况都被逐条挡下，报错信息带出对应的先验依据
  - `ruler` 校验单测：合法 full/fixed 通过；`n=150` 低于下限 / `mode=full` 但 n≠-1 /
    cmd 不是官方脚本 / 缺 `why` 四种情况被挡下
  - 尺子注入链路：`eval_policy_text()` 在 golden_init.json 存在时动态读取 ruler
    并渲染进所有节点；普通实验节点的 prompt 里出现「统一尺子…全程不得更换」段落
- **完整 dry run 跑通新循环**（8h 预算、`MAX_NODES=22`、9 个实验节点）：
  - `bootstrap → climb_wide → reignite` 按 `recipe_stable()` 与剩余预算正确切档
  - 每轮三个候选的坐标**互斥**（实测 `Inference.Hyperparams` / `Training.Data` /
    `Harness.Memory` → 下一轮轮转到未试过的坐标）
  - `reignite` 轮只出 strategy 层坐标（`Model.*` / `Training.Method`），
    并排除了最近被采纳节点的坐标
  - lens 按 `(round + idx)` 轮转，`lens_histogram` 覆盖到 6 个视角
  - Measurement 在第 1、4、6、8 轮触发共 4 次 = `MAX_MEASURE` 上限，
    间隔满足 `MEASURE_GAP=2`（**修掉了改造中途出现的"平台期每轮都重跑度量"问题**）
  - 采纳阈值0.01（全量评测定死为 IMPROVE_EPS）；Δ=0 的节点不被采纳，
    连续两次后进入 reignite
  - `summary.json` 四张直方图齐全（`step_size: large 6 / medium 3`，
    `layer: strategy 6 / exec 3`）
  - `finalize` 正确把 best（0.46 vs baseline 0.30）拷成 `final_model`
- **所有渲染出的 prompt 无未替换占位符**（扫了全部 `*.prompt.md`，含 Golden Run 的
  `golden_run.prompt.md` / `engineer.prompt.md`；并确认普通实验节点的
  `{{RUN_MODE_NOTE}}` / `{{CHECK_MODE_NOTE}}` 渲染为空、不会串味）
- **guard 单测（含 2>&1 放行）**：`bash timer.sh 2>&1 | head` / `python evaluate.py
  --help 2>&1 | tail` / `ls -R research/harness_src 2>&1` 放行；`echo x > evaluate.py`
  / 重定向+保护名 / `rm research/state.json` / 非 hypothesis 阶段 `torchrun` 拦；
  写 `evaluate.py` 拦、写 `research/step_policy.json` 拦、
  `research/protocol.md` 在 `measurement` 阶段可写 / 在 `hypothesis` 阶段拦、
  `diagnose.py` 在 `hypothesis` 拦而在 `measurement` 放行、`torchrun` 在 `experiment` 放行
  而在 `judge` 拦、`rm research/state.json` 拦、写节点产物放行

### 8.2 改造之前就验证过、且本次未破坏的部分

- **旧版单进程 Golden Init 曾真实跑通（codex + gpt-5.6-terra，32 分钟预算，实际用
  10.7 分钟）**，当时的产物全部合规：
  - `protocol.json` 带 9 条 file+line 证据，并且**纠正了一个我原本写错的假设**：GSM8K 官方
    scorer 用的是 inspect 的 `match(numeric=True)` 末尾数字匹配，**不是** `ANSWER:` 正则
  - `dev/dev.jsonl`（当时是 100 条）、`scripts/eval_dev.{sh,py}`、
    `dev/last_run.jsonl`（逐样本，965KB）
  - 真实基线：dev accuracy 0.38、官方 `evaluate.py --limit 50` 也是 0.38（两者同向）
  - 失败归因分桶：推理错 0.26 / 格式错 0.18 / 抽取失败 0.09 / 截断 0.05 / 复读 0.04
  - `bottlenecks.md`、`literature.md`、`base_plan`
- **GPU 阶段隔离 live 生效**：非实验阶段子进程里 agent 实测看到 `CUDA=[]`
- **Step2 三路并行猜想 + judge 真实跑通**（旧版 `assets/loop_probe.py`，复用已有 Golden Init
  产物、不做训练，全程 4 分 7 秒）：3 个候选并行 64/70/83 秒返回，judge 真的去核对了证据
  （自己数出 `last_run.jsonl` 100 条 / 38 正确 / 32 format_error / 9 extract_fail / 5 truncated，
  并逐个查了 5 个被引用的样本 id）。**注意：这次验证用的是旧版打分式 judge，
  新的"硬规则 + 两两对比"版本没有 live 跑过。**
- 通过 `run_research.sh` 的完整链路空跑，**两个后端都 rc=0**：host.env → uv env →
  `prepare_hf_layout.sh` → `run_in_container.sh` → `{codex,claude}_research/solve.sh` →
  `research_common/entry.sh` → 编排器 → `final_model` 交付并被 harness 拷进 `$OUT_DIR`。
  `claude_research` 正确命中 `run_in_container.sh` 的 `claude*` 鉴权分支，`codex_research` 不命中
- Claude Code 2.1.157 **会加载** `--settings` 指定的文件并**真的执行钩子**
  （用 SessionStart 探针验证，钩子落地了文件）
- SIGTERM → `final_model` 落地、退出码 143

### 8.3 早期 live 测试中发现并修掉的两个 bug（仍在）

1. **网关错误误判导致重跑整个节点。** 原来只要事件流里出现 `all nodes exhausted` /
   `overloaded_error` 字样就判为网关故障并退避重跑。实测 Golden Init `rc=0`、产物齐全，
   却因为 agent 自己的一条 `command_execution` 输出里含这个字样而被判 `ERR=api`。
   现在改成：**产物文件是唯一的成功判据**——拿到合法产物就立即返回，只有
   「没产物 + 事件级错误」才退避重试（`run_node`）。
2. **`golden_init.json` 存在但 `state.json` 没有 baseline 时，delta 永远算不出来**，
   导致任何节点都不会被采纳。现在 `adopt_golden_into_state()` 会从 `golden_init.json`
   补齐 baseline 与 golden 节点记录，并从 `golden_run.json` 恢复主干 best——
   否则续跑会把主干丢掉、退回基座重开。

### 8.4 未验证（诚实说明）

- **本次改造（Golden Run + 统一尺子）都没有 live 回合。** 具体没跑过：
  Measurement 的四类分面是否真能产出可用的 diagnose.py；坐标分配下模型会不会大量 `abstain`；
  两两对比式 judge 的实际选择质量；`min_viable_h` 降级路径；统一尺子的真实单次耗时
  （vLLM 启动 2-3 分钟 + 子集评测，n=300 时预计 ~10 分钟）；
  synthesis 能否定出合理的 ruler（全量 vs 子集的判断依据是 baseline 实测耗时）
- **Opus 5（claude 后端）第一次 live（2026-09-08 19:02，已废弃）暴露并修复**：
  literature 在 30 分钟硬预算点被 SIGKILL，死前刚说 "Writing the artifacts now"——
  契约没落盘拖死整个 Golden Init。修复：literature/protocol 增加"边调研边落盘
  （先写 partial 契约再补充）+ 时间感知计划"纪律；guard 的 `2>&1` 误拦放行。
  该 run 的官方全量基线已完成且复现稳定（0.4284 vs 前日 0.427）。
- **Golden Run 没有 live 回合，且它是本次改造里风险最集中的一处**。具体没验证：
  - synthesis 在真实 CLI 下能否稳定产出通过 `validate_recipe_contract()` 的完整配方——
    它比原来的契约大得多；字段不全现在不再判废重来，而是由 `repair_recipe()` 用默认值
    补齐（这消除了"开局收尾"的风险，但换成了"关键决定被默认值代做"的风险）
  - synthesis 不持卡，`budget.total_est_min` / `vram_est_gb` 只能靠先验估算，
    真实偏差有多大未知（缓解措施是 Golden Run 的超时不夹到这个估值上）
  - 官方全量评测的真实耗时（gsm8k 1319 条）能否装进 35% 的预算上限
  - 配方里的 `cmd` 是 synthesis 写的、脚本尚不存在，Engineer_1 能否照它实现出来
- 三路并行 Golden Init（尤其独立的限时 literature WebSearch）尚未跑过真实回合；
  旧版单进程 Golden Init 的 live 结果不能替代这次验证。literature 契约新增了 `paradigms`
  必填项，真实 CLI 能否稳定产出这个字段没验证过
- **claude 后端没有跑通过一次真实回合。** 测试期间 yy.dbh 网关的整个 Claude 系列
  （opus-4-6/4-7/4-8、opus-5、sonnet-4-6）在 OpenAI 与 Anthropic 两种协议下都返回
  `503 all nodes exhausted`（同期 `gpt-5.6-terra` 正常）。
  顺带一个事实：网关的 `/v1/messages` **能**转发 `gpt-5.6-terra`（实测通），
  所以 claude 后端配 gpt 模型技术上跑得通——但那违反仓库的 CLI↔模型配对惯例，不作为默认
- 因此 **claude 后端 PreToolUse 拦截的完整回合**（模型发起工具调用 → 被 exit-2 挡回 →
  模型看到错误信息）未跑通。已验证的是链路两端：settings 被加载 + 钩子会执行 + guard 判定正确
- `--agents` 内联子 agent 是否被正常接受未确认（JSON 形状符合 `--help` 文档，
  但 live 调用因 503 超时）。已留 `RESEARCH_USE_AGENTS=0` 开关兜底
- 需要 GPU 的部分（训练、vLLM 评测）完全没跑过；一次真实的多小时端到端运行也没跑过
- 运行前提：本机 GPU 必须空闲。`check_cuda.py` 只要发现卡上有别人的进程就会让整个 run
  在训练阶段前 exit 2（实测撞到过一次：GPU 0 上有别人 53GB 的进程）

## 9. 没有实现的部分（按风险排序）

1. **`research/scripts/diagnose.py` 没有代码，是让 Step1 Measurement 节点生成的。**
   （2026-09-07 调整：不再自建 dev 集/评测器——统一尺子就是官方 `evaluate.py` 加固定参数，
   彻底消灭「自建实现复现不了官方语义」这一整类 bug（曾把 `max_tokens=4000` 写死、与
   `generation_config` 取小行为不一致，被 synthesis 判 fail 后整个 run 直接终止）。
   Step1 只建纯 CPU 的日志分面分析器，并由其 `rerun.is_baseline` 登记尺子锚点。）
   分数本身不再有这个风险（尺子就是官方 `evaluate.py`，不是每次现写的）；剩下的风险
   是 diagnose.py 算错了分面数字会误导猜想说错方向——但它不产生分数，影响面小得多。
2. **没有 seed 重复与方差估计。** 采纳阈值全量评测定死为0.01、子集评测挂到`se(n)`上，
   但仍是**单次**评测：没有同一配方跑多 seed 取均值，也没有配对比较。
3. **污染防护仍是软的（但比旧版硬一点）。** Step3 实验节点与 data-builder 的提示词会
   **要求**在存在 `contamination_check.py` 时必须运行，并把命中数写进 `experiment.md`。
   但 `myptbench` **没有**把官方的
   `contamination_check.py` 与 `test_data.json` 拷进任务目录（见 12.6 第 3 条），
   所以在本机跑时这条路径实际走不到，退化成通用规则。**要真正闭环，得先把官方查重器接上。**
4. **codex 后端没有接钩子。** 文件级保护（不许改 `evaluate.py`/`templates`）在 codex 后端
   只有提示词约束 + 编排器侧校验，没有当场拦截。codex 0.135 支持 PreToolUse 钩子但走 plugin
   `hooks.json` 那一套。GPU 阶段隔离不受影响（那是靠清空 `CUDA_VISIBLE_DEVICES` 做的）。
5. **没有 token / 成本预算控制。** 只按 wall-clock 分配；`--max-budget-usd` 没接，
   用量只记录不约束。实测量级（codex + gpt-5.6-terra）：Golden Init 一个节点
   253 万 input / 1.8 万 output tokens；一轮「3 猜想 + judge」合计约 98 万 input / 2.1 万 output。
   本次改造把 Measurement 变成可能多次触发、prompt 也变长了，
   **token 账只会更高**；跟单次 baseline 比「相同预算」时这点必须补。
6. **judge 只放行 1 个候选，不支持并行跑多个实验**（单卡限制）。多卡切分未实现，
   `NUM_GPUS>1` 时不会自动做实验并行。
7. **节点不能续跑。** `--resume` / `--session-id` 续跑没用上，实验节点会在**自报估时**
   或全局墙钟处被停，长实验无法跨节点接续（估时含 ≥20% 余量是 prompt 级要求）。
   中间步骤无硬超时的代价：CLI 真挂死会耗到全局墙钟才被 harness 的 NUM_HOURS
   定时器杀掉——靠 SIGTERM→finalize 保住 final_model，但会损失后面所有轮次。
8. **结构化输出没用 `--json-schema` / `--output-schema`**，靠「写文件 + 字段校验 + 带反馈重试」。
   比 schema 强制弱，但产物文件同时也是归档证据，取舍如此。
9. **调度切档不再看配方成没成立。** `recipe_stable()` 已随步长分档一起删除，
   `schedule()` 现在只看 `no_improve_streak` 和剩余预算比例切 explore / reignite / polish。
   代价是"主干还没建起来"和"主干已稳定"走同一套 regime，区分全靠提示词。
10. **黄金流程挖掘（流程记录.md 的第二件事）完全没写。** `stream.jsonl` 全都留着了，
    但没有任何离线分析代码去从轨迹里挖范式。注意两个后端的事件 schema 不同
    （claude 是 `type=result` 等，codex 是 `thread.started` / `turn.completed` 等），
    以后写分析工具要分别处理。
11. **GC 按节点顺序而不是磁盘水位。** 磁盘满了不会自救。
12. **编排器不主动做「官方分数 vs dev 分数」的一致性校验**，只在 Golden Init 的汇总节点
    要求它检查一次同向性，之后各节点自己确认。
13. `PROMPT`（harness 生成的原始任务提示词）本 agent **不使用**——每个节点的提示词由编排器
    自己组装。官方那 9 条规则已经按"谁可能违反"分发进各节点的 prompt（见 12.2 的覆盖矩阵），
    但如果 harness 侧的 `prompt.txt` 以后改了，这里不会自动跟随。

## 10. 与 baseline 的对比方式

- baseline 用**未修改的**同 CLI 单次 agent：codex 后端对 `agents/codex`，
  claude 后端对 `agents/claude`。同 `NUM_HOURS`、同 `AGENT_CONFIG`、同 `MODEL`、
  同 `TASK`、同卡数——CLI 和模型都要配对，否则比的是 CLI 而不是流程
- 最终分数只认官方 `evaluate.py --limit -1 --json-output-file`，不看 dev 分数
- 至少 3 次重复；`summary.json` 的三张直方图用来量化「策略是否比 baseline 更不 trivial」：
  - `target_histogram`：改了哪些操作空间坐标（是否只会动一个坐标）
  - `regime_histogram`：explore / reignite / polish 各占多少轮
  - `layer_histogram`：exec / strategy 的比例
  （`step_size` 与 `lens` 已随步长分档删除，不再统计）
- 记录两种预算：wall-clock（天然对齐 `NUM_HOURS`）与总 token（本版本只记录不约束）

## 11. 怎么删

```bash
cd /root/ComateProjects/chats/myptbench
rm -rf PostTrainBench/agents/{research_common,codex_research,claude_research} run_research.sh
```
然后不再传 `AGENT=codex_research` / `claude_research`。原有跑法完全不受影响。

会漏到目录外的东西（都由启动参数控制，删的时候一起删）：run 目录整体
`$RUNS_ROOT/<task>_<model>_<agent>_<agent-config>_<时间戳>/{work,out,traces}` 与 `TRAJ_PORT`；
`HF_HOME` 是共享缓存，Golden Init 下载的数据集会留在里面。codex 后端还会用到 `$HOME/.codex`，
但那是 `run_in_container.sh:216` 本来就会覆盖的，与本 agent 无关。

## 12. 背景：我们在刷的 bench 是什么，官方要求是什么

（本节事实来自上游仓库 `/root/ComateProjects/chats/PostTrainBench` 的 `README.md`、`AGENTS.md`、
`src/eval/general/prompt.txt`、`src/eval/general/get_prompt.py`、`src/eval/tasks/*/info.json`。）

### 12.1 PostTrainBench 在测什么

**PostTrainBench: Can LLM Agents Automate LLM Post-Training?**
（[posttrainbench.com](http://posttrainbench.com/)，`aisa-group/PostTrainBench`）测的不是模型本身，
而是**CLI agent 能不能自动化 LLM 后训练**：给 agent 一个 base LLM、一个 benchmark、一份
`evaluate.py`，外加 **10 小时 1×H100**，最后按它交出来的后训练模型在该 benchmark 上的分数计分。
本质上是在评估 agent 做 AI R&D 的能力——这也是我们这条流水线要改进的对象。

- 官方 scaffold 四个：**Claude Code / Codex CLI / Gemini CLI / OpenCode**
- 官方 benchmark 七个：AIME 2025、Arena Hard Writing、BFCL、GPQA、GSM8K、HealthBench Easy、HumanEval
- 我们目前只跑 GSM8K + `Qwen/Qwen3-4B-Base`

### 12.2 官方给 agent 的 9 条规则（上游已从 7 条扩到 9 条）

`src/eval/general/prompt.txt` 原文要点（我们把它按"谁可能违反"分发进了各节点 prompt，
2026-09-10 决策删掉 `common_context.md` 之后不再有单一副本）：

1. 无人交互，全自主
2. 最多 `{num_hours}` 小时，`bash timer.sh` 查剩余
3. **不得用该 benchmark 的测试集问题或答案做训练**
4. **不得从具体 test item 派生训练数据**（改写、扰动、拿测试题做生成 seed、手写近邻题
   都算污染，即使没有逐字重叠）；匹配一般风格/格式/领域/难度是允许的
5. **不得修改 `evaluate.py` 与 `templates/`**
6. 只在当前目录及子目录下工作（HF 缓存例外）
7. `final_model` 必须能在起始环境（同一套包）里跑起来
8. **只能从指定 base 模型微调**，禁止下载它的 instruct 版本或任何其他模型
9. **不得用环境里的任何 API key 直接调外部 LLM**（例如让大模型帮你生成训练数据）；
   驱动 agent 自己和跑官方评测的 key 不算。自己用别的方式生成数据是允许的

（第 10 条是条件性的：只有 `arenahardwriting` / `healthbench` 会拿到 `OPENAI_API_KEY`，
且只准用于评测打分。）

**分发到哪些节点**（第 1、9 条在全部 10 个节点里；其余按"这个节点有没有可能违反"给）：

- 第 3、4 条（测试集 / test item 派生）：除 protocol 外全都有。protocol 只读评测源码，
  不碰数据；measurement 只给第 4 条（它做诊断、不训练）
- 第 5 条（不改 `evaluate.py` / `templates/`）：除 literature 与 judge 外全都有
- 第 7 条（`final_model` 可加载）：synthesis / plan / engineer1 / engineer2（写配方、
  设计实验、产出与检查交付目录的那几个）
- 第 8 条（只能微调指定 base）：literature / synthesis / hypothesis / plan / engineer1
- 第 9 条在 baseline 与 measurement 里额外点名了"别让外部大模型帮你给 bad case 分类"——
  这是这两个节点最可能踩到 `api_usage_judge` 的方式

改任何节点 prompt 时对着这一段核一次；漏掉第 9 条的代价是整个 run 的分数回落到 baseline。

另外官方明确写了 **`Internet access is unrestricted`** —— 上网是被允许的，见 12.5。

### 12.3 官方的执行环境（和我们不一样的地方）

官方用 **apptainer `-c --cleanenv --pid --no-init`** 起沙箱：容器内 home 是 `/home/ben`，
工作目录 `/home/ben/task`，产物必须落在 `task/final_model/`。关键点是
**沙箱不继承任何宿主环境变量**，只有 allowlist 里的 key 会被 `--env` 显式注入：

- `agents/<agent>/api_keys.json` → 该 agent 允许拿到的 provider key
  （`claude` 只有 `ANTHROPIC_API_KEY`，`codex` 只有 `CODEX_API_KEY`，订阅制 agent 是 `[]`）
- `src/eval/tasks/<task>/info.json` → `required_api_keys`，只有
  `arenahardwriting` / `healthbench` 需要 `OPENAI_API_KEY` 来跑打分 judge

沙箱拿到的是两者的**并集**。所以在 GSM8K 上，官方的 agent 手里**没有** `OPENAI_API_KEY`。

### 12.4 官方的四个反作弊 judge（哪个真的影响分数）

`src/judges/run_judges.sh`（由 `src/run_task.sh` 在 agent 跑完后调用）依次跑四个 agent-as-judge：

- **`data_contamination_judge`**（GPT-5.4 / codex CLI）—— 测试集使用、评测篡改、模型替换、
  违规微调。结论 `judgement_gpt5_4.json`
- **`api_usage_judge`** —— 是否以不允许的方式调用外部 LLM API。
  **这个会真的改分数**：一旦 flag，`scripts/collect.py` 把该 run 的分数回落到 baseline
- **`ptb_lookup_judge`** —— 是否去查 PostTrainBench 本身（官网、GitHub 仓库、已公开的历史轨迹，
  用来抄策略）。结论归档用，但 `collect.py` 一旦发现 flag 就直接报错，不会悄悄放过
- **`general_judge`**（GPT-5.6 Terra）—— 开放式扫「未知的未知」：提前停止、额度耗尽、
  harness/基建故障、以及前三个 judge 覆盖不到的新型 reward hacking。纯归档

官方还**主动把去污染工具交给 agent**：benchmark 的 `test_data.json` 加
`src/judges/judge_tools/contamination_check.py` 会被 `run_task.sh:140-141` 拷进 agent 沙箱 home，
`get_prompt.py:77-95` 相应加一段「Decontamination Tool」。也就是说 agent 可以用**和 judge 完全
同一个 checker** 自查训练数据；judge 阶段会重新拷一份，防止 agent 改过的版本被用来自证清白。

### 12.5 官方对「上网」和「数据来源」的真实立场

这条直接推翻了一个容易犯的直觉：**官方不禁网，而是事后审计**。

- prompt 里写的是 `Internet access is unrestricted`
- 真正被禁的是「查 PostTrainBench 本身抄策略」，而且是靠 `ptb_lookup_judge` 事后抓，不是事前拦
- 数据来源有**显式白/黑名单**，写在 `src/eval/tasks/<task>/info.json` 的
  `allowed_data_examples` / `disallowed_data_examples`。**注意这是判分细则，不是给 agent 的说明书**：
  `get_prompt.py` 只读 `required_api_keys`，这两个列表从不进 agent 的 prompt；
  吃它们的是 `src/judges/get_judge_prompt.py:128-145`，填进 `data_contamination_judge/prompt.md`
  的占位符。所以官方 agent 也看不到具名清单，只能靠 prompt 里的通用规则 +
  `contamination_check.py` 自查：
  - `gsm8k`：允许 `gsm8k training subset`、`meta-math/MetaMathQA`（因为它只含 gsm8k 训练集）；无禁用项
  - `aime2025`：允许 `Maxwell-Jia/AIME_2024`；禁 `opencompass/AIME2025`、`yentinglin/aime_2025`、`MathArena/aime_2025`
  - `humaneval`：允许 `Muennighoff/mbpp`；禁 `openai/openai_humaneval`、`evalplus/humanevalplus`
  - `bfcl` / `gpqamain`：无允许项，分别禁 `gorilla-llm/Berkeley-Function-Calling-Leaderboard`、`Idavidrein/gpqa`

这份清单的存在本身说明一件事：**agent 是按一份自己看不到的细则被判的**。所以安全边界只能保守推断，
不能指望官方告诉你哪个 HF 数据集能用。我们的 `step0_literature_review.md` 因此不注入具名清单，
只注入通用边界 + 禁查列表。

### 12.6 我们这份复现与官方的五处差距

`myptbench` 是从同事的 `ptbench` 复制出来的本机适配版（见 `myptbench/CHANGES.md`），
它本身又是上游的一个较早/裁剪过的快照。差距按影响排序：

1. **没有沙箱隔离，环境继承一切。** 官方 `--cleanenv` + allowlist 意味着 GSM8K 上 agent 拿不到
   `OPENAI_API_KEY`；我们是宿主机直跑，`host.env` 里的 key、代理、一切变量原样继承下去。
   **等于我们的 agent 手里有一把能调外部 LLM 的 key**，而这正是 `api_usage_judge` 要抓的行为。
   本编排器对此只有提示词层面的禁令（官方第 9 条，已分发进全部 10 个节点 prompt），没有技术限制。
2. **judge 只有一个而且默认关。** `myptbench/PostTrainBench/src/` 下只有
   `disallowed_usage_judge/` 一个（上游是 `src/judges/` 四个），`run_research.sh` 默认
   `RUN_JUDGE=0`。所以我们本地跑出来的分数**没有经过官方的任何反作弊闸门**。
3. **去污染工具没接上。** `myptbench` 里没有 `contamination_check.py` /
   `model_identity_check.py`，`prompt.txt` / `get_prompt.py` 也没有「Decontamination Tool」段落
   （本机 `prompt.txt` 仍是旧 7 条）。`gsm8k/test_data.json` 在仓库里，但
   `run_in_container.sh` 不会把它拷进任务目录。**这就是第 9 节第 3 条的真正来源——
   不是官方没提供工具，是我们的复现没带上。** 提示词已经写好了「有就必须用」的分支，
   把上游那两个文件拷进任务目录就能立刻生效。
4. **`info.json` 整个缺失。** `myptbench/.../tasks/gsm8k/` 只有 `benchmark.txt`、`evaluate.py`、
   `test_data.json`，没有 `info.json`，myptbench 里也没有任何代码去读它。对 agent 侧影响其实是零
   （gsm8k 的 `required_api_keys` 本来就是空的，白/黑名单本来也不进 agent prompt）；真正丢的是
   **judge 侧的判分细则**——就算把上游四个 judge 接回来，`fill_data_contamination` 也拿不到清单，
   只能退化成通用规则。属于「以后要补 judge 时才会咬人」的欠账。
5. **硬件是 H800 不是 H100**，且 `get_prompt.py` 生成的提示词里写的也是 H800。
   分数只能内部横向比，不能对外声称对齐官方榜单。但是单卡情况下，h800和h100一致，h800是h100砍半nvlink的产物，不太影响单卡性能。


