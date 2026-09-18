# golden-recipe：写 Golden Run 工单（`golden_recipe` + `ruler`）

## 何时用

- synthesis 动笔写 `golden_recipe` / `ruler` 前必读：字段骨架与各默认值的取值区间只在这里定义。
- 第 1 节是硬前提；第 2–6 节是**默认值**，用本任务的 `protocol.md` / `bottlenecks.md` /
  `literature.json` 实例化，每处偏离在 `rationale` 里给依据。

---

## 1. 训练与评测必须是同一个形状

模型在评测时认不认得眼前的输入，比超参对不对更早决定成绩。

**四层都要对齐，缺一层就是不一致：**

- **上下文层**：有没有 system 轮？是指令还是 few-shot 示范、几条、取自哪个 split？
- **包装层**：输入被什么模板包住（前缀指令、重复提醒、结尾提示词）？
- **正文层**：期望的回答风格与长度（要不要推理过程、多长）？
- **收尾层**：终答用什么标记、后面紧跟什么终止符、终止符之后还有没有东西？

**怎么写清**（纯读动作，不重渲染模板、不起服务）：从 `protocol.md` 的「prompt 形状」/
「答案抽取与判分」抄下四层形状 → 到 `logs/` 读一条真实请求核对（不一致以日志为准，
差异写进 `findings`）→ 把逐字模板写进 `data.answer_format`，让工程师照着拼就行。

**两条最容易漏：**

- `data.system_prompt` 是决策不是格式字段。写 `null` 等于声明"训练与评测上下文层一致"；
  评测端有 system 轮却写 `null`，要在 `rationale` 里说明为什么不伤收尾。也可以让一部分
  样本带与评测同构的示范前缀（示范只取 train split），两种上下文下都能正常收尾。
- **终答必须是输出的最后一段**：正文 → 终答标记 → 终止符，中间不留空行或尾随内容。
  训练序列里终答后面出现过什么，评测时模型就在同一位置续写什么。

自检：模型在评测里第一次见到的东西，训练时见过吗？训练样本终答之后是什么？

## 2. 训练默认值（偏离要给理由）

- **范式：全参 bf16 SFT**。LoRA/QLoRA 常输在输出协议层（学不会发终止符、答案后吐随机
  token），不是能力层；要同时改"输出格式 + 收尾行为 + 任务能力"就用全参。只学单一窄
  模板的任务可用 LoRA，必须填 `paradigm.lora_justification`。
- `lr = 1e-5`（区间 1e-5 ~ 2e-5）、`scheduler = cosine`、`warmup_ratio = 0.03`、
  effective batch 16 ~ 32、`epochs = 1 ~ 2`（**写 >3 会被编排器压回 2**）。
- **bs/ga 先大后小**：effective batch 定了之后 per-device bs 往大试（比如从 8/16 起步），
  爆显存再减半。grad_accum 只是凑 effective batch，不影响吞吐；
  wall time ∝ tokens / (bs × 吞吐)，bs 压小是白烧时间。
- **`max_seq_len` 从 `protocol.md` 记的有效生成上限反推**，不要用固定值。
  `packing` 只在长序列任务开。
- loss 只算 completion（prompt 掩掉）；多轮任务只在最后一个 assistant turn 放 loss。
- 工程默认：FlashAttention-2 + gradient checkpointing；词表大时用 chunked/fused CE
  防 logits OOM。
- **generation_config 两步分离**：训练脚本只 `save_pretrained`；交付四键由独立脚本
  json.dump 直写。transformers ≥4.5x 保存时 `validate(strict=True)` 会拒绝
  `temperature=0`，混进 save 就是训练白跑不落盘（实录重训 50min）。
- checkpoint 按官方 `evaluate.py` 分数选，不按 val loss。
- **单阶段起步**：`paradigm.stages` 默认一个 SFT。同目标续训（在已训模型上接着喂新数据）
  普遍负向，要更多数据就合并数据集**从基座重训**。真有把握开局就上 RL，把那一阶段直接
  写进 `stages`；RL 只在 reward 可程序化验证时用，lr 比 SFT 低一个数量级（1e-6 ~ 3e-6）。

## 3. `generation_config` 必须显式写全

评测 harness 通常只传 `max_tokens`，采样参数取自模型目录的 `generation_config.json`；
推理引擎**不读 `do_sample`**，只读 `temperature` / `top_p` / `top_k` /
`repetition_penalty`。基座配置常缺 `temperature`，服务端就退回 `1.0`，实测能吃掉
几个到几十个百分点——这是最便宜的失分点。

- 四个采样键全部显式写出。缺了编排器会补贪心默认值，但那不见得适合本任务（见第 3 节末）。
- **`temperature` 不要无脑写 0**：短答案任务贪心最优；长输出任务贪心会陷入死循环，
  且超长生成在 offline/server 两种模式间不可复现。按本任务输出长度决定并在
  `rationale` 里说明。
- `eos_token_id` 写**数组**，覆盖 `protocol.md`「终止与生成上限」列出的全部 id（从 protocol 读，
  不要抄示例数字）。注意：在裸基座上补终止符常是 no-op——它压根不产生那个 token，
  真正让它生效的是**训练时就让模型输出该终止符**，两件事要一起做。
- `max_new_tokens` 与 harness 请求值**取小者**生效，必须 ≥ 本任务需要的输出长度。

## 4. 数据

- **规模**：2×10⁴ ~ 1.5×10⁵ 条，10⁵ 附近见顶。先做 2–5 万条格式完全对齐的数据打通
  闭环，只有错误分析显示"格式零失败、剩余全是能力错"才扩量。
- **来源风格优先于名气**：与评测端同构的短/中等 trace 收尾信号最干净；风格差很远的长
  trace 先剪裁到评测生成预算内。
- **四项必做筛选**：正确性/执行验证过滤 → 去污染并报出扫描数与命中数 →
  长度上限对齐生成预算 → 问题级去重。
- 拒绝采样/RFT 只在能执行验证时有效，且必须复用官方 scorer 的抽取路径。
- 数据只能来自 train split 或外部合法来源，不得从具体 test item 派生。

## 5. 交付管道要写进 `steps`

交不出可评测的模型是最常见的失败方式，多数直接原因是推理服务起不来：目录缺配置文件、
权重分片不全、tokenizer 缺失、只留 adapter 没 merge。所以 `steps` 必须有一步
"保存后立刻验证：目录完整性 → 真起一次推理服务 → 在提交目录本体上跑官方
`evaluate.py`"，耗时计入 `budget.total_est_min`。冒烟请求用评测端的同构上下文——
裸问题能通过，却测不出收尾问题。

## 6. 预算与降级

- Golden Run 用**官方全量**打分（与基线同刻度）：`budget.eval_mode` 必须是
  `official_full`、`eval_n` 填 `-1`；全量评测很慢，耗时如实计入预算。
- `total_est_min` 含 ≥20% 余量，估时慷慨：风险是"没跑完"和"交不出模型"，不是"跑得慢"。
- `fallback` 给降级版本（减 epochs / 减数据量），**不许改训练范式**。
- 用力程度匹配任务空间：baseline 分数与失败分桶显示优化空间很小时，配方就该便宜
  （做对协议 + 小规模格式对齐），把预算留给循环。

## 7. 统一尺子 `ruler`（定稿后全程不变）

循环阶段每次评测都用官方 `evaluate.py` 加固定参数，**不自建 dev 评测器**。

- **全量优先**，判据是 `全量耗时 × 预计评测轮数` 装不装得起；装不起才退固定子集，
  `n` 不得小于编排器注入的下限。
- `cmd` 写进本脚本真实存在的 flag：`--limit`（`-1` 或子集 n）、`--max-tokens`、
  `--max-connections` / `--gpu-memory-utilization`（若脚本有），`--model-path` /
  `--json-output-file` 写占位符。**速度参数也钉进尺子**，不要留给下游自己摸——
  默认并发/显存极保守，裸跑会把窗口吃光。取值优先级：baseline `eval_cmd_used` 实测过
  且没把窗口吃爆的那组 > `protocol.md`「评测命令与速度参数」里给的建议值。
  脚本没有的 flag 不要硬加。
- 定稿后不得更换（换尺子会让历史节点不可比）。下游只换 `--model-path` 与
  `--json-output-file`，其它 flag 逐字照抄。

## 8. 字段骨架（字段名照抄，值按本任务实例化）

```json
{
  "rationale": "为什么是这份配方：追溯到 protocol/bottlenecks/literature 的哪条证据，每处偏离默认值的理由，以及四层形状对齐的决策",
  "paradigm": {
    "method": "full_sft",
    "lora_justification": null,
    "stages": [{"name": "sft", "objective": "ce_completion_only"}]
  },
  "data": {
    "sources": [{"name": "数据集名", "split": "train", "n": 30000, "why": "对应哪个瓶颈"}],
    "filters": ["正确性验证：...", "去污染：8-gram，报出扫描与命中数", "长度截到 Xk 对齐生成上限", "问题级去重"],
    "answer_format": "训练样本的逐字模板：上下文层 + 包装层 + 正文层 + 收尾层",
    "system_prompt": "具体内容，或 null（写 null 就是在声明上下文层一致）",
    "target_n": 30000
  },
  "hyperparams": {
    "lr": 1e-5, "scheduler": "cosine", "warmup_ratio": 0.03,
    "epochs": 2, "per_device_bs": 8, "grad_accum": 4,
    "max_seq_len": 2048, "packing": false, "bf16": true,
    "loss_mask": "completion_only", "attn": "flash_attention_2"
  },
  "generation_config": {
    "eos_token_id": ["从 protocol.md 读终止符，数组"],
    "temperature": 0.0, "top_p": 1.0, "top_k": 1,
    "repetition_penalty": 1.0, "max_new_tokens": 2048
  },
  "checkpoint_selection": "official_eval_score",
  "steps": [
    {"name": "构造并筛选数据", "cmd": "python ...", "est_min": 30},
    {"name": "全参 SFT", "cmd": "...", "est_min": 75},
    {"name": "写 generation_config", "cmd": "...", "est_min": 2},
    {"name": "交付管道验证：目录完整性 + 起服务 + 同构上下文冒烟", "cmd": "...", "est_min": 15},
    {"name": "官方全量评测", "cmd": "python evaluate.py --model-path <node>/model --limit -1 <沿用 ruler 的速度参数> --json-output-file ...", "est_min": 15}
  ],
  "budget": {"total_est_min": 167, "vram_est_gb": 70, "eval_mode": "official_full", "eval_n": -1},
  "success_criterion": "官方全量分数高于 baseline.official，且截断/格式失败率明显下降",
  "abort_criterion": "科学或可行性判据（loss 不降、过滤后数据为空等），不要写成时间配额",
  "fallback": "跑不完时的降级版本：减 epochs 或减数据量，不改训练范式"
}
```

`cmd` 写成工程师能读懂意图的命令（脚本还不存在没关系，写清它该做什么、输入输出在哪）。
不要写"根据情况选择合适的数据集"这类留白——留白等于让工程师替你决策。

## 9. 编排器会替你补的项（写全了就没人代你决定）

下面这些缺了不会让 Golden Init 重来——编排器按本手册的默认值补齐后照样跑 Golden Run
（`repair_recipe` / `repair_ruler`）。但补出来的是通用默认值，不是针对本任务的决定：

- `golden_recipe` 缺 `rationale` / `paradigm` / `data` / `hyperparams` /
  `generation_config` / `steps` / `budget` / `success_criterion` / `abort_criterion`
  任一项；
- `paradigm.method` 未填，或非 `full_sft` 且 `lora_justification` 为空（会被改回 `full_sft`）；
- `hyperparams` 缺 `lr` / `epochs` / `max_seq_len`，或 `epochs > 3`（会被压回 2）；
- `generation_config` 缺 `eos_token_id` / `temperature` / `top_p` /
  `repetition_penalty`（补成占位说明 + 贪心默认值，**这条最容易吃分**）；
- `steps` 为空，或任一 step 缺 `name` / `cmd`（会被换成一份"自己按字段实现"的空壳）；
- `budget.total_est_min` 未填，或 `budget.eval_mode` 不是 `official_full`；
- `ruler` 缺 `mode` / `n` / `cmd` / `why`，或 `mode` 与 `n`/`cmd` 不一致
  （会退成 baseline 实测命令的全量刻度）。

`fallback` 与 `top_k` 也一样，按第 3、6 节写全。
