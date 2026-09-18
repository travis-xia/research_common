# golden-recipe：写 Golden Run 工单（`golden_recipe` + `ruler`）的完整手册

## 何时用（触发条件）

- **Step0-D synthesis 动笔写 `golden_recipe` / `ruler` 之前必须先读完本文**：
  完整字段骨架、每个默认值的取值区间与证据、编排器的**判废清单**只在这里有定义。
  凭印象写会缺字段或写错区间，直接判废、整个 Golden Init 重来。
- Golden Run 的工程师 / 检查节点想确认"配方为什么这么写"时可读；
  配方本身以注入的 `GOLDEN_INIT` 为准，本文只解释规则与依据。

第 0 节是最高优先级。第 1–9 节是**默认值**——用 `protocol.json`、`bottlenecks.md`、
`literature.json` 的本任务证据实例化，每处偏离都要在 `rationale` 里写依据。
第 10–11 节是字段骨架与判废清单，落盘前对着自查。

---

## 0. 训练与推理必须是同一个形状

后训练能不能拿到分，先取决于模型在评测时**认不认得眼前的输入**。超参写得再对，
只要训练时的输入形状与评测时不一致，模型就会在一个没见过的上下文里失去它学到的
行为边界——最典型的后果是**不会收尾**：在终答之后继续生成、复读，直到打满生成上限。

**实测代价**：有一次开局训练因此报废。配方把 `system_prompt` 写成 `null`、
`answer_format` 只对齐了终答行，而评测端每条请求都带一个 10-shot system 轮
（每条示范都以"终答行 + 空行 + 下一题"的形式排列），训练样本却是裸问题、无 system 轮。
结果 **95.5% 的响应打满生成上限、195/200 复读终答行**，官方全量分数
43.1% → 32.9%，**低于未训练的基座**。反面证据同样硬：同 benchmark 一条 91.2% 的
高分 run 明确写"12% 的训练样本带随机抽取的 few-shot system preamble
**to match eval conditions**"；另一条 90.5% 的 run 把"训练模型输出终止符 + 设为 EOS"
单独记为一步 38%→80% 的跃升。**收尾行为与上下文对齐是同一件事的两面。**

### 评测端的形状是一个整体，至少四层

- **上下文层**：有没有 system 轮？里面是什么（指令 / few-shot 示范 / 几条 / 来自哪个 split）？
- **包装层**：待评测的输入被什么模板包住（前缀指令、重复提醒、结尾提示词）？
- **正文层**：期望的回答风格与长度（是否要推理过程、多长）？
- **收尾层**：终答用什么标记、后面紧跟什么终止符、终止符之后还有没有东西？

缺任何一层就是不一致。

### 怎么写清（全是纯读动作，不重渲染模板、不起服务）

1. 从 `protocol.json` 的 `prompt_shape` / `answer_extraction` 抄下四层的完整形状；
2. 到 `logs/`（baseline 的逐样本日志）里读**一条真实请求**核对；与 protocol 描述
   不一致时以日志为准，差异写进 `findings`；
3. 把训练样本的**逐字模板**写进 `data.answer_format`——四层都要写到，让工程师
   照着就能拼出一条样本，不需要自己判断。

### 两条最容易写漏的

- **`data.system_prompt` 是一个决策，不是格式字段**。写 `null` 等于声明"训练与评测在
  上下文层一致"。评测端有 system 轮而仍写 `null`，必须在 `rationale` 里说明为什么这个
  偏差不会伤到收尾行为。对齐也不必二选一——让一部分样本带上与评测同构的示范前缀
  （示范只能取自 train split，有高分 run 取 ~12%），模型在带示范与不带示范两种
  上下文里都能正常收尾。
- **终答必须是输出的最后一段**：正文之后紧接终答标记，终答之后立刻是终止符，中间不留
  空行或尾随内容。训练序列里终答后面出现过什么，评测时模型就会在同一位置续写什么。

### 自检（三个问题都要答得出来）

- 模型在评测里**第一次见到**的东西（system 轮、示范、包装、结尾提示词），训练时见过吗？
- 训练样本的终答之后是什么？评测时模型在同一位置最可能续写什么？
- **答案格式对齐只是一半，上下文对齐是另一半。**

---

## 1. 默认值（跨 7 个 benchmark 收敛，直接用；偏离要给理由）

- **范式：全参 bf16 SFT**。26/27 条高分 run 是全参；LoRA/QLoRA 在 7/7 个 benchmark 上
  都是低分组标记，没有一条高分 run 用 QLoRA 交卷。
  失败机制要记住：LoRA 输在**输出协议层**（学不会发终止符、答案后吐随机语种 token），
  不是知识/能力层——所以凡是需要同时改"输出格式 + 停机行为 + 任务能力"的任务，
  必须全参。只有当任务只要求学**一个窄输出模板**时，LoRA 才是可接受的（但也没有收益上的
  理由）；这种情况必须填 `paradigm.lora_justification`，否则编排器判废。
- `lr = 1e-5`（区间 1e-5 ~ 2e-5）：7/7 高分组众数。低分组众数是 `2e-4`。
- `scheduler = cosine`，`warmup_ratio = 0.03`。
- `epochs = 1 ~ 2`。**3 epoch 过拟合在 4 个 benchmark 上有明确记录**，编排器把 >3 判废。
- effective batch = 16 ~ 32（`per_device_bs` × `grad_accum`）。
- **`max_seq_len` 按生成预算定，不要用固定值**。低分组集中在 1024/2048——
  长 CoT 任务把 `max_seq_len` 设成 2048 是典型的低分特征。要从 `protocol.json` 的
  有效生成上限反推。
- `packing` 只在长序列任务开（长 CoT 任务上高分组开启率明显更高，短输出任务上反而是
  低分特征）。
- loss 只算在 completion 上（prompt 掩掉）。多轮任务只在**最后一个 assistant turn** 放 loss。
- 工程默认：FlashAttention-2 + gradient checkpointing；词表很大时用 chunked/fused CE
  避免 logits OOM。
- **checkpoint 选择按官方 `evaluate.py` 分数，不按 val loss**。
  按 val loss 选（`load_best_model_at_end`）全库校正后是 **−2.2 个百分点**。

## 2. 单阶段起步；补数据要从基座重训，不要续训

**同目标续训（在已训模型上接着喂新数据）在 5 个 benchmark 上都是负向操作**：
23.3%→20.0%、74.0%→72.7%、84.1%→67%、38%→28% 都是真实记录。
正向的多阶段**一律是换了目标函数**（SFT→GRPO / SFT→on-policy DPO / SFT→执行验证 RFT）。

所以 `paradigm.stages` 默认只有一个 SFT 阶段。需要更多数据时，做法是合并数据集
**从基座重训**，而不是加一个续训阶段。

## 3. RL/DPO：默认 `rl_decision: "skip"`，四个前置条件全满足才开局就上

只有约 26% 的高分 run 在最终交付里保留了 RL/偏好阶段，而且增益强依赖 reward 可验证性。
反例很多：知识型多选任务上 GRPO 完全无迁移、竞赛数学上前后持平甚至下降、
工具调用上 DPO reward margin ≈0.005 被整段弃用、开放生成上 DPO 低于 SFT。

四个前置条件（要在 `rl_precondition` 里逐条回答，缺一条就 skip）：

1. reward **可程序化验证**（答案精确匹配 / 单测执行）。judge 型任务不满足。
2. SFT 已到平台**且格式零失败**。弱 SFT 上直接 RL 会把格式冲掉。
3. **先筛掉 reward 饱和的 prompt**——不筛的话组内优势全为 0，等于白跑。
   有高分 run 的做法是先对训练题各采 k 个样，只保留通过率非饱和的那部分。
4. RL 阶段 lr 比 SFT **低一个数量级**（1e-6 ~ 3e-6）。

开局阶段的正确姿势通常是 `skip`：先用 SFT 把主干建起来，RL 留给循环里的 large 步长猜想
去提——那时才有"SFT 已到平台"这个前提。

## 4. 数据

- **规模**：主区间 2×10⁴ ~ 1.5×10⁵ 条，约 10⁵ 处明显见顶（有 4 条独立的饱和证据：
  再加 20 万条掉 1.3pp、5.1 万→6.1 万完全打平等）。
  **先做 2–5 万条格式完全对齐的数据打通闭环**；只有当错误分析显示"格式零失败、
  剩余全是能力错"时才扩到 10⁵。有一条高分 run 反而用了最小的数据量（2.4 万条），
  因为它把预算花在"每题只留最短的正确 trace"而不是堆量。
- **来源风格优先于来源名气**：优先挑与评测端**同构**的短/中等 trace（题面风格、
  推理长度、终答形式都接近），它给出的收尾信号最干净；风格差很远的长 trace 即使
  质量更高，也要先剪裁到评测生成预算内再用。
- **必做的筛选**（7/7 都有高分 run 证据）：
  1. **正确性/执行验证过滤**（答案可验证就验证，代码就跑单测）；
  2. **去污染并报出具体数字**（扫了多少条、命中多少条）。认真去污染**不牺牲成绩**——
     去污染证据充分的组在 7/7 个 benchmark 上分位都更高，不要因为怕掉分而敷衍；
  3. **长度上限与生成预算对齐**（把过长的 trace 剪到能在评测生成上限内输出完）；
  4. **问题级去重**。
- **可选**：拒绝采样/RFT 只在能执行验证时有效，而且**必须复用官方 scorer 的抽取路径**；
  只校验最终答案、不校验过程质量的 RFT 会稀释数据（有 79.3%→75.3% 的记录）。
  难度分层在数学/科学类任务上有效，在开放生成上没人做。
- 格式与上下文对齐见第 0 节——那是本节的前置条件，不是可选项。
- 数据只能来自 train split 或外部合法来源，**不得从具体 test item 派生**。

## 5. `generation_config` 必须显式写全（最便宜的高频失分点）

评测 harness 只传 `max_tokens`，采样参数取自模型目录的 `generation_config.json`；
而推理引擎**不读 `do_sample`**，只读 `temperature` / `top_p` / `top_k` /
`repetition_penalty`。基座自带的配置通常缺 `temperature`，服务端就退回 `1.0`——
实测能吃掉 7 个百分点到 80 多个百分点（有 4%→91%、85.5%→93.0%、+14% 等多条记录）。

- `eos_token_id` 必须是**数组**且覆盖 `protocol.json` 里列出的全部终止符。
  基座自带的往往只有一个值（例如只有 `<|endoftext|>`），而评测模板每轮结束发的是
  `<|im_end|>` 之类的另一个 token，引擎不认就会让模型在答案后继续自言自语。
  **各基座的终止符集合不同，从 `protocol.json` 读，不要照抄示例数字。**
  注意：在**裸基座**上补第二个终止符通常是 no-op（它压根不产生那个 token），
  真正让它生效的是**训练时就让模型输出该终止符**——两件事要一起做。
- 上面四个采样键**全部显式写出**，缺一个编排器就判废。
- `max_new_tokens` 会与 harness 请求的值**取小者**生效，所以它必须 ≥ 本任务需要的输出长度，
  否则长输出会被悄悄截断。
- **`temperature` 不要无脑写 0**：短答案任务上贪心最优；但长 CoT 任务上贪心会陷入推理
  死循环（有记录：0/30 的样本能闭合 `</think>`，改成 0.6 后终止率从 ~13% 升到 ~63%；
  另有实录：收尾行为没训牢时，贪心把 95% 的响应变成复读打满上限），
  而且超长生成下贪心在 offline/server 两种模式之间不可复现。按本任务的输出长度决定，
  并在 `rationale` 里说明。

## 6. 任务性质决定配方该多重（用 baseline 分数和瓶颈自己判断）

不同任务的可优化空间差别很大，配方的"用力程度"要匹配：

- **可验证的短答案推理**：空间大，值得做完整的数据构造 + 全参 SFT；`temperature=0`。
- **长 CoT 竞赛数学**：天花板低、截断是主要瓶颈。重点在长度预算与终止行为，
  不要指望靠堆数据翻盘；解码可能需要采样而非贪心。
- **知识型多选**：优化空间极小（有的任务四个基座的中位数全部挤在随机线附近，
  全场最高分也只比随机高十几个点）。配方应当**便宜**：做对协议 + 小规模格式对齐，
  不要做英勇的大规模训练——把预算留给循环。
- **代码**：执行验证过滤有效，数据量级可以偏大。
- **工具调用**：往往是"全对或全错"的脆性任务，赌注几乎全在能停机 + 输出合法结构上。
  小数据 + 窄模板即可，重点是协议对齐。
- **LLM-judge 开放生成**：reward 不可程序化验证，RL 只能走 on-policy 偏好数据；
  数据量级偏小（约 10⁴）。

**怎么归类由你根据 `protocol.json` 的答案抽取方式、`bottlenecks.md` 的失败分桶和
baseline 分数自己判断**，不要套用上面的标签名。

## 7. 交付管道要写进 `steps`（全库头号失败模式）

**11.5% 的 run 最终交不出可评测的模型**，其中一半直接原因是推理服务起不来：
目录缺 `config.json` / 权重分片不全 / tokenizer 缺失 / 只留了 LoRA adapter 没 merge /
gemma 系基座缺 `preprocessor_config.json`。

所以 `steps` 里**必须**有一步是"保存后立刻验证：目录完整性 → 真起一次推理服务 →
在提交目录本体上跑官方 `evaluate.py`"，并把它的耗时算进 `budget.total_est_min`。
冒烟请求要用**评测端的同构上下文**（带 system 轮/示范的真实形状）——
"1+1" 式的裸问题能通过，却测不出收尾问题。

## 8. 时间规划

- Golden Run 用**官方 `evaluate.py --limit -1` 全量**打分（与基线同刻度），
  `budget.eval_mode` 必须是 `official_full`、`eval_n` 填 `-1`。全量评测很慢，
  把它的耗时如实算进预算。
- `total_est_min` 要含 ≥20% 余量。**估时慷慨一点**：总墙钟通常是宽松的，
  真正的风险是"没跑完"和"交不出模型"，不是"跑得慢"。
- 必须给一个 `fallback` 降级版本（减 epochs / 减数据量），
  **降级不许改变训练范式**——跑不完等于零分，但换个范式就不是同一个配方了。

## 9. 定统一尺子（`ruler`，循环阶段全程不变）

循环阶段的每一次评测都用官方 `evaluate.py` 本身加固定参数——**不再自建 dev 评测器**
（自建实现复现不了官方语义，曾烧掉过一次完整 run）。要把尺子一次性定死：

- **全量优先**（能不切就不切）。判据：baseline 刚跑过官方全量，它的实测耗时就是最好的
  证据——循环阶段预计要做十次量级的评测，用 `全量耗时 × 预计轮数` 评估是否装得起；
- 全量装不起才退**固定子集**：`--limit N`，N 不得小于编排器注入的下限
  （子集太小噪声会淹没信号：n=150 时 se≈0.04，n=300 时 se≈0.029，采纳阈值挂在这个 se 上）；
- cmd 基于官方 `evaluate.py`，固定 `--limit` 与 `--max-tokens`（与官方默认一致），
  `--model-path` / `--json-output-file` 写占位符；速度类参数
  （`--max-connections` / `--gpu-memory-utilization`）允许节点自行调整，不影响语义；
- protocol 的 `ruler` 提议是起点，用 baseline 实测耗时校正后定稿；
- **尺子一经定稿全程不得更换**——中途换尺子会让所有历史节点不可比；
- `mode=full` 时 `n` 写 `-1` 或 `official_full_n` 均可（语义相同，编排器自行核对）；
  `cmd` 里必须是 `--limit -1`。

## 10. `golden_recipe` 字段骨架（字段名照抄，值按本任务实例化）

```json
{
  "rationale": "为什么是这份配方：逐条追溯到 protocol/bottlenecks/literature 的哪条证据，每处偏离默认值的理由，以及格式与上下文对齐的决策",
  "paradigm": {
    "method": "full_sft",
    "lora_justification": null,
    "stages": [{"name": "sft", "objective": "ce_completion_only"}],
    "rl_decision": "skip",
    "rl_precondition": "逐条回答四个前置条件；skip 时写清哪一条不满足"
  },
  "data": {
    "sources": [{"name": "数据集名", "split": "train", "n": 30000, "why": "对应哪个瓶颈/哪条文献证据"}],
    "filters": ["正确性验证：...", "去污染：8-gram，报出扫描与命中数", "长度截到 Xk 对齐生成上限", "问题级去重"],
    "answer_format": "训练样本的逐字模板：上下文层 + 包装层 + 正文层 + 收尾层，与 protocol 的 prompt_shape / answer_extraction 对齐",
    "system_prompt": "具体内容，或 null；写 null 就是在声明训练与评测上下文层一致——评测端有 system 轮却写 null，必须在 rationale 里给理由",
    "target_n": 30000
  },
  "hyperparams": {
    "lr": 1e-5, "scheduler": "cosine", "warmup_ratio": 0.03,
    "epochs": 2, "per_device_bs": 8, "grad_accum": 4,
    "max_seq_len": 2048, "packing": false, "bf16": true,
    "loss_mask": "completion_only", "attn": "flash_attention_2"
  },
  "generation_config": {
    "eos_token_id": ["从 protocol.json 读，数组"],
    "temperature": 0.0, "top_p": 1.0, "top_k": 1,
    "repetition_penalty": 1.0, "max_new_tokens": 2048
  },
  "checkpoint_selection": "official_eval_score",
  "steps": [
    {"name": "构造并筛选数据", "cmd": "python ...", "est_min": 30},
    {"name": "全参 SFT", "cmd": "...", "est_min": 75},
    {"name": "写 generation_config", "cmd": "...", "est_min": 2},
    {"name": "交付管道验证：目录完整性 + 起服务 + 同构上下文冒烟 + 官方 evaluate.py", "cmd": "...", "est_min": 15},
    {"name": "官方全量评测", "cmd": "python evaluate.py --model-path <node>/model --limit -1 --json-output-file ...", "est_min": 45}
  ],
  "budget": {"total_est_min": 167, "vram_est_gb": 70, "eval_mode": "official_full", "eval_n": -1},
  "success_criterion": "官方全量分数高于 baseline.official，且截断/格式失败率明显下降",
  "abort_criterion": "科学或可行性判据（loss 不降、过滤后数据为空等），不要写成时间配额",
  "fallback": "跑不完时的降级版本：减 epochs 或减数据量，不改变训练范式"
}
```

`cmd` 要写成工程师能直接读懂意图的命令（脚本还不存在没关系，写清它该做什么、
输入输出在哪）。**不要写"根据情况选择合适的数据集"这类留白**——留白等于让工程师替你决策。

## 11. 编排器判废清单（命中任意一条，整个 Golden Init 重来）

`golden_recipe`：

- 缺 `rationale` / `paradigm` / `data` / `hyperparams` / `generation_config` /
  `checkpoint_selection` / `steps` / `budget` / `success_criterion` / `abort_criterion` /
  `fallback` 任一字段；
- `paradigm.method` 非 `full_sft` 且没填 `lora_justification`；
- `paradigm.rl_decision` 未显式写（`skip` 也要写）；
- `hyperparams` 缺 `lr` / `scheduler` / `warmup_ratio` / `epochs` / `per_device_bs` /
  `grad_accum` / `max_seq_len` 任一项，或 `epochs > 3`；
- `generation_config` 的四个采样键不全，或 `eos_token_id` 不是数组；
- `steps` 为空，或 `budget.total_est_min` 未填；
- `budget.eval_mode` 不是 `official_full`。

`ruler`：缺字段、`mode` 与 `n`/`cmd` 不一致（`full` 必须是 `--limit -1`）、
固定子集的 `n` 小于编排器注入的下限。
