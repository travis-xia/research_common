# Qwen3-4B-Base：七个任务 Top-2 训练 Recipe

数据源：`traces/corpus_index.json`（PostTrainBench 官方分）+ 对应 `raw/*.json` 里 agent 的收尾报告 / 实际训练命令。

基座一律是 `Qwen/Qwen3-4B-Base`。每个任务取官方 accuracy 最高的两条 **未污染** run（`contamination.flagged = false`）。并列时按索引顺序取前两条，不按 stderr 再排序。

BFCL 有 4 条官方 1.00，但全部被判污染，未列入。HumanEval 第二名的 agent 在 epoch 1 结束时撞上 session limit，交上去的是保险 checkpoint，不是它计划中的最终模型。

## 总表

| 任务 | 名次 | 官方分 | agent | 一句话 recipe |
|---|---|---|---|---|
| aime2025 | 1 | 0.300 | claude-fable-5 | AceReason 最短正确轨迹全参 SFT + 末 3 ckpt soup |
| aime2025 | 2 | 0.300 | claude-opus-4-8 | OpenR1-Math 精简轨迹全参 SFT，2 epoch |
| arenahardwriting | 1 | 0.862 | claude-fable-5 | 30B 写作蒸馏 SFT + repetition_penalty 1.05 |
| arenahardwriting | 2 | 0.852 | claude-fable-5 | 30B 蒸馏 SFT + on-policy DPO |
| bfcl | 1 | 0.980 | claude-fable-5 | xLAM/APIGen 格式对齐全参 SFT |
| bfcl | 2 | 0.980 | kismet-0715 | xLAM + Glaive + 合成约定数据全参 SFT |
| gpqamain | 1 | 0.397 | claude-opus-5 | OpenScience R1 轨迹全参 SFT + 反重复采样 |
| gpqamain | 2 | 0.366 | gpt-5.5 | 公开科学 MCQ 的 LoRA SFT（只学答案） |
| gsm8k | 1 | 0.912 | claude-opus-5 | OpenMathInstruct-2 全参 SFT + GRPO + soup |
| gsm8k | 2 | 0.907 | claude-fable-5 | 91k CoT SFT → RFT → GRPO |
| healthbench | 1 | 0.567 | claude-fable-5 | Baichuan-M2 蒸馏 SFT + on-policy DPO + soup |
| healthbench | 2 | 0.494 | claude-opus-5 | 本地 30B 合成多轮医疗对话，全参 SFT |
| humaneval | 1 | 0.854 | gpt-5.6-sol | OpenCodeInstruct 高分样本续训 + 权重插值 |
| humaneval | 2 | 0.841 | claude-fable-5 | Evol/OSS/self-oss/KodCode 混合全参 SFT（只跑完 1 epoch） |

---

## aime2025

评测：AIME 2025，30 题。两条官方分都是 **0.300**（stderr 0.085）。

### 1. 0.300 — claude-fable-5[1m]

`claude_non_api_max_claude-fable-5_1m__10h_run1__aime2025_Qwen_Qwen3-4B-Base_17337591`

交付物是同一次全参微调最后 3 个 checkpoint（step 980 / 1120 / 1152）的权重平均。agent 自己在开发集上的汇总数是 3 次全量 21/90 = 23.3%，官方单次抽到 30.0%。

- **数据**：`nvidia/AceReason-1.1-SFT` 的数学子集（DeepSeek-R1-0528 轨迹）。每道题只留最短的正确轨迹（858k 条冗余轨迹压成 139,695 道独立题），只要整数答案，对 AIME 2025 做 8-gram 去污染，轨迹上限 13.3k token（评测生成预算 16k），再按长度分层抽到 **23,804 条 / 150M token**。
- **格式**：prompt/completion 与评测用的 `qwen3.jinja` 逐字节对齐；completion 以 `ANSWER: <int><|im_end|>` 结束；completion-only loss。
- **训练**：TRL packing（BFD），ctx 16384，padding-free FlashAttention-2，Liger kernel，全参，lr 1.5e-5 cosine，1 epoch。单卡 H100 约 4h27m，9.4k tok/s。
- **解码**：`generation_config.json` 写双 EOS `[<|im_end|>, <|endoftext|>]`，Qwen3 thinking 采样 T=0.6 / top_p 0.95 / top_k 20。vLLM 会把这份 config 当成默认采样。
- **丢掉的**：长轨迹二阶段续训 20.0%；二阶段 soup 13.3%；更宽的 4-ckpt soup 20.0%；温度 0.3 也是 20.0%。能在 16k 内写完的题大约 60% 正确，超长截断是主要失分。

### 2. 0.300 — claude-opus-4-8

`claude_non_api_max_claude-opus-4-8_10h_run1__aime2025_Qwen_Qwen3-4B-Base_17315103`

官方 0.300。agent 自己的开发集（n=4）是 22.5%，确认评测单次抽到过 13.3%，它按开发集留下了 run-2。

- **数据**：`OpenR1-Math-220k` 的 DeepSeek-R1 蒸馏竞赛数学轨迹，格式化成评测期望的 `<think>…</think> … ANSWER: N<|im_end|>`。对 30 道 AIME 2025 去污染，删掉 1 条重叠。
- **交付**：run-2，**2 epoch、约 18k 条精简轨迹**。run-1 是 1 epoch、16k 条，官方 16.7%。run-3 在 OMR AIME 数据上再训 1 epoch，开发集 20.8%，已回退。
- **训练**：全参，不用 Liger；8-bit Adam + flash-attention，batch size 2，序列长度封顶，用来躲过 152k 词表的 logits 显存。
- **解码**：temp 0.6 / top_p 0.95 / top_k 20，EOS `[151645, 151643]`。贪心解码在 16k 生成上离线与 server 模式不可复现（开发集 23.3% → 官方 16.7%），已放弃。

---

## arenahardwriting

评测：Arena-Hard-v2.0 Writing，对 Qwen3-1.7B 的 winrate，gpt-5-mini 裁判，A/B 两边都打。

### 1. 0.862 — claude-fable-5[1m]

`claude_non_api_max_claude-fable-5_1m__10h_run2__arenahardwriting_Qwen_Qwen3-4B-Base_17331422`

官方 0.8624 ± 0.010。agent 在全量 250 题上自报 0.870 ± 0.010。

- **数据**：约 2 万条 `Qwen3-30B-A3B-Instruct-2507-FP8` 的回复，教师带 “exceptional writer” system prompt，SFT 时把这段 system **丢掉**。约 30% 非英语（ru/zh/ja/de/fr/es 等），因为测试集大约三分之一不是英语。prompt 来自 WildChat 写作意图、r/WritingPrompts、no_robots，再加约束型合成题。对测试集做 8-gram + Jaccard + CJK char-gram 去污染。
- **训练**：按评测 chat template 的渲染结果做 SFT（无 system、无 think block）。
- **解码**：`generation_config.json` 里的采样默认值；**repetition_penalty 1.05 单独值 +0.10**（32 题上 0.750 → 0.858），主要是掐掉续写到 1.5 万 token 的退化循环。
- **丢掉的**：教师排序的 on-policy DPO，32 题 0.833，不高于 SFT+采样。RAFT 自蒸馏掉到 0.672。

### 2. 0.852 — claude-fable-5[1m]

`claude_non_api_max_claude-fable-5_1m__10h_run1__arenahardwriting_Qwen_Qwen3-4B-Base_17381375`

官方 0.8516 ± 0.011。agent 全量 250 题自报 0.835 ± 0.011。

- **数据**：23.6k 写作 prompt（WildChat-1M 写作过滤、9 种语言；Magpie-Qwen2.5-Pro；no_robots；r/WritingPrompts；aya），75/25 拆给 SFT / DPO。教师是 `Qwen3-30B-A3B-Instruct-2507`，滤完 **16.8k** 条 SFT。
- **SFT**：评测 chat template，completion-only loss。152k 词表 logits OOM 用 liger fused CE 解决，liger 只在训练环境。
- **DPO**：SFT 模型每条 prompt 采 4 个样本，30B 教师做**成对比较**而不是 1–10 打分（打分均值饱和在 9.5，分不出高下）。5,296 对，相对 SFT +14.5pt（32 题）。
- **解码**：temp 0.7 / top_p 0.8 / top_k 20 / repetition_penalty 1.05。
- **丢掉的**：同一批 pair 再做一轮 DPO（−6）。RAFT 从同一批数据只涨 +5.8，不如 DPO。

---

## bfcl

评测：BFCL `exec_simple`，100 题。官方 1.00 的 4 条全部 `contamination.flagged = true`，下面是未污染最高的两条，都是 **0.980**。

### 1. 0.980 — claude-fable-5[1m]

`claude_non_api_max_claude-fable-5_1m__10h_run2__bfcl_Qwen_Qwen3-4B-Base_17411268`

基线 0.00。自报路径：3k smoke 91% → v2 96% → v3–v6 **98%**。

- **契约**：单工具、恰好一次 `<tool_call>`，name+arguments 与标准答案 dict 相等。vLLM + `qwen3.jinja` + hermes parser。训练文本与 server `/tokenize` 逐字节对齐。
- **数据**（去污染 0 命中）：xLAM/APIGen-60k 的单调用子集 27.8k（清洗并按 schema 做类型强制）；Synth-APIGen 15k；再加自己写的合成器（`lambda x:` 紧凑表达式、百分数转小数、散文转数组、公司名转 ticker、货币转 ISO、可选参数“写或不写”的对比对）。
- **训练**：全参 bf16，flash-attn，8-bit AdamW，completion-only loss，2 epoch，lr 1e-5。
- **解码**：`temperature: 0.0`，EOS `<|im_end|>`，保证一次 tool call 后停下。
- 最后 2 个错被它判断成可选参数约定的标注不一致，多轮都停在 98%���

### 2. 0.980 — kismet-0715[1m]

`kimi_claude_k3-0715_1m__10h_run3__bfcl_Qwen_Qwen3-4B-Base_17417460`

交付的是 v2 SFT 的 **epoch 2**（`ckpt_v2/checkpoint-5886`）。全量分数：v1-ep2 0.95 → v2-ep1 0.97 → **v2-ep2 0.98** → v2-ep3 0.97。

- **数据**：xLAM-60k（原仓库 gated，用镜像 `lockon/xlam-function-calling-60k`）、Glaive-function-calling-v2，加上程序合成的数学工具数据和约定数据。规模 24.8k + 2.7k + 18.7k + 0.9k。目标是单个 `<tool_call>`，用评测 template 渲染，做了类型强制和“丢掉默认值回显”的过滤。`contamination_check.py` 对每个版本都是 0。
- **训练**：全参 bf16，flash-attention-2，completion-only loss，3 epoch，lr 1e-5 cosine，有效 batch 16。留下的是 epoch 2，不是 epoch 3。
- **解码**：`eos_token_id: [151645, 151643]`，`temperature: 0.0`。
- **丢掉的**：model soup、两次约定数据续训、DPO（reward accuracy 0.62，margin ≈ 0.005，评测 0.97）、string-flag 续训。都没超过 0.98。

---

## gpqamain

评测：GPQA 全集 448 题。

### 1. 0.397 — claude-opus-5

`claude_non_api_claude-opus-5_10h_run1__gpqamain_Qwen_Qwen3-4B-Base_17415823`

官方 0.3973 ± 0.023。agent 在全集上自报 v3 = 0.411 ± 0.023（基线约 0.15，n=100）。v1 只有 0.125，低于随机。

- **数据**：`nvidia/OpenScience` 的四选一子集（约 391k 科学 MCQ，带 DeepSeek-R1 轨迹）。渲染成 inspect-ai `multiple_choice(cot=True)` 的原文字符串，套 `qwen3.jinja`，目标是 `<think>…</think>\n\nANSWER: X`。原始标签 36% 是 B、14% 是 D，轨迹里点名了选项字母，所以不能打乱选项，改为下采样把字母平衡过来。16 万条对 GPQA 测试集去污染，0 命中。
- **训练**：全参，TRL SFT，bf16，flash-attn-2，BFD packing @ 6144，completion-only loss，8-bit Adam。v2：按长度分层的 40k（一半是长轨迹），lr 1e-5，全集 0.402。v3：再用 26k 新样本续训，lr 6e-6，全集 0.411，这是交付的。v2+v3 soup 是 0.385，没留。
- **解码**（比加数据更关键）：v1 有 77% 生成撞上 16k 上限、在复读里永远不写 `ANSWER:`。`generation_config.json` 把 repetition_penalty 从 1.0 调到 1.06、温度从 0.6 调到 0.9，GPQA 截断率 77% → 4.5%。

### 2. 0.366 — gpt-5.5

`codex_non_api_xhigh_gpt-5.5_10h_run1__gpqamain_Qwen_Qwen3-4B-Base_17134242`

官方 0.3661 ± 0.023。这条没有收尾报告，recipe 来自实际命令和判罚说明。

- **数据**：`build_sft_data.py` 只从非 GPQA 公开集拼 `data/sft_quick`：`TIGER-Lab/MMLU-Pro`、`cais/mmlu` auxiliary_train、`openlifescienceai/medmcqa`、`GBaker/MedQA-USMLE-4-options`、`allenai/sciq`、`derek-thomas/ScienceQA`、`allenai/ai2_arc`、`allenai/openbookqa`、`deepmind/aqua_rat`。最终 run 用的 train.jsonl 是 23,519 条，val 800 条。`contamination_check.py` 报 0。
- **训练**：LoRA SFT，不是全参。交付命令是

```bash
python train_lora_sft.py \
  --train-file data/sft_quick/train.jsonl --val-file data/sft_quick/val.jsonl \
  --max-length 2048 --epochs 1 --batch-size 16 --grad-accum 2 \
  --lr 2e-4 --lora-r 32 --lora-alpha 64 \
  --assistant-suffix eos --completion-mode answer \
  --output-dir runs/lora_quick_answer_r32_seed101 --seed 101
```

  `--completion-mode answer` 表示监督信号是答案而不是长推理。基座默认 `Qwen/Qwen3-4B-Base`，`merge_lora.py` 合成 `merged_quick_answer_r32_seed101` 后整模拷进 `final_model`。

---

## gsm8k

### 1. 0.912 — claude-opus-5

`claude_non_api_claude-opus-5_10h_run1__gsm8k_Qwen_Qwen3-4B-Base_17415829`

官方 0.9121 ± 0.008。agent 在默认参数、n=150 上自报 soup 0.927；n=300 时各候选在 0.920–0.937 之间，它认为分不出胜负，选了 soup 降方差。

- **数据**：92k，来自 `nvidia/OpenMathInstruct-2`。73.6k 独立 `augmented_gsm8k`、7.4k GSM8K **train**、约 14k 从 MATH 派生的题。`\boxed{}` 改成 harness 要求的最后一行 `ANSWER: $ANSWER`。8 万条对测试集去污染，0 命中。12% 的样本带一段从 GSM8K train 抽的 few-shot system，用来对齐评测条件。
- **SFT**：全参，1 epoch，lr 1e-5 cosine，bf16。152k 词表的 logits 会 OOM，所以只在被监督的位置上算 loss，LM head 分块重算（`ChunkedLossTrainer`）。约 10k tok/s，73 分钟。
- **GRPO**：先用 vLLM 在 2 万道训练题上每题采 4 次，筛出 6.5k 个 pass rate 没饱和的 prompt，只在这些上做 GRPO。第 1 轮 lr 1e-6、240 step（reward 0.70→0.72）；第 2 轮 lr 3e-6、160 step（0.72→0.81）。第 2 轮 step 80 掉到 0.877，这个 lr 不稳定。
- **解码**：vLLM 忽略 `do_sample`，只读 `generation_config.json` 里的 `temperature`。基座因此其实是温度 1.0。把 `temperature` 写成 0.0，同一 SFT 模型从 0.855 到 0.930（+7.5pt）。EOS 设成 `<|im_end|>`。
- **交付**：SFT、GRPO-1 step 150、GRPO-2 step 120 的均匀权重平均。

### 2. 0.907 — claude-fable-5[1m]

`claude_non_api_max_claude-fable-5_1m__10h_run2__gsm8k_Qwen_Qwen3-4B-Base_17333772`

官方 0.9067 ± 0.008。agent 用默认 `evaluate.py` 复测两次，自报 0.913–0.920。基线（10-shot）0.413。

| 阶段 | 做法 | 自报（limit 150，贪心） |
|---|---|---|
| SFT v1 | 91k math-CoT，2 epoch | 0.873 |
| RFT | 加上自蒸馏的正确解 | 0.893 |
| GRPO r1 | 120 step | 0.900 |
| **GRPO r2（交付）** | 再 200 step | **0.920**（400 题上 0.9175） |
| GRPO r3 | 再 150 step | 0.920，打平，没换 |

- **数据**：GSM8K train 原题 + MetaMathQA 的 GSM 子集 + OpenMathInstruct-2，渲染成和评测 prompt 逐字节相同的格式。约 10% 的行带评测同款 system message。对测试集 8-gram 去污染。
- **RFT**：每道训练题采 k=8，pass@8 = 98.9%，留下 29k 正确且多样的解，混着 replay 和新的增强题再训。
- **GRPO**：TRL，vLLM colocate，精确匹配奖励，DAPO loss，prompt 只用 GSM8K train。这是单段最大涨幅（+2.7pt）。
- **解码**：`temperature: 0.0`，`eos=[<|im_end|>, <|endoftext|>]`。相对默认温度 1.0 值好几个点。

---

## healthbench

评测：HealthBench 全集 245 条，gpt-5-mini 裁判。

### 1. 0.567 — claude-fable-5[1m]

`claude_non_api_max_claude-fable-5_1m__10h_run1__healthbench_Qwen_Qwen3-4B-Base_17387514`

官方 0.5667 ± 0.020。agent 全集自报 0.553 ± 0.022，基线约 0.13。n=128 上 soup 是 0.591，高于两个父模型 3–4pt。

- **数据**：不用 HealthBench 内容。1.2 万条对话种子 = 按公开 HealthBench 主题加权的合成场景（沟通 / hedging 偏多）+ 3.5k 真实消费者健康问题。多轮上下文 5–11 条消息，由 Qwen3-8B 生成，中间几轮助手回复故意写得一般。最后一轮答案从 **Baichuan-M2-32B-GPTQ-Int4** 蒸馏（只做推理，不把它当训练基座），9.2k 条通过校验。
- **SFT v1**：7k 对话，loss 只在最后一轮助手，`qwen3.jinja`。2 epoch 最好；3 epoch 过拟合。n=32 上 0.63，但 instruction_following 接近 0。
- **SFT v2**：再加 2.1k 补丁数据（严格格式、多问一句、急诊、数据类任务，做过合规过滤）。n=128 上 0.561。
- **DPO**：1,091 对 on-policy。教师按每段上下文写 rubric，学生采 6 条，本地裁判加格式合规奖励；被截断的啰嗦回复当 rejected。instruction_following 0.27→0.73，但 context_seeking 退了。
- **交付**：`0.5·SFT + 0.5·DPO` 的 soup。

### 2. 0.494 — claude-opus-5

`claude_non_api_claude-opus-5_10h_run2__healthbench_Qwen_Qwen3-4B-Base_17419574`

官方 0.4941 ± 0.022。agent 全集自报交付模型 0.5017 ± 0.023，基线 0.1047。更大的数据（14.5k、1 epoch）全集 0.4987，统计上打平，留了更小的那个。

- **数据**：本地 vLLM 起 `Qwen3-30B-A3B-Instruct-2507-FP8`，三遍生成，不调外部 API。(1) 手写种子网格合成多轮健康对话：7 个主题 × 约 190 个话题 × 14 个人设 × 20 种语言 × 12 种场景 × 22 种转折，轮数是奇数、以用户结尾，对齐 benchmark 结构。(2) 用编码五条评分轴的 system prompt 生成“理想”末轮回答。(3) 教师自己改自己的草稿，补临床细节。中位回答长度约 2.0k → 2.5k 字符，这是它认为最大的数据质量收益。
- **过滤**：15,441 → 14,530。教师在斯瓦希里语、约鲁巴语、阿姆哈拉语上退化，这些语言从种子里删了，并加了重复 / 词汇多样性过滤。每个训练集 `contamination_check.py` 都是 0。
- **训练**：全参 bf16，loss 只在最后一轮助手，对话按 `templates/qwen3.jinja` 渲染。交付的是 **8k 数据、2 epoch**。同一 8k 上 2 epoch 胜过 3 epoch（配对 96 题上 0.522 vs 0.476）；14.5k 上 1 epoch 胜过 2 epoch。
- **解码**：`eos_token = <|im_end|>`，贪心，让 vLLM 停得下来并且可复现。

---

## humaneval

评测：HumanEval 164 题，pass@1。

### 1. 0.854 — gpt-5.6-sol

`codex_non_api_max_gpt-5.6-sol_10h_run2__humaneval_Qwen_Qwen3-4B-Base_17403817`

官方 0.8537 ± 0.028。这条是 Codex，没有收尾报告。下面是从训练命令和判罚说明里对上的谱系。开发集快评（limit 150、max-tokens 2048）上，最终 soup 是 **0.8533**。

数据主源是 `nvidia/OpenCodeInstruct`，streaming 读取、不把 6.4GB 全集下完，按 `llm_judgement` 留高分，并做目标清洗。Magicoder 被调查过，最终高分续训把 `--magic-count` 设成了 0。HumanEval 的 entry point 和 10-gram 会在写数据前剔除；三份实际使用的数据 `contamination_check.py` 都是 0。

| 阶段 | 命令要点 | 作用 |
|---|---|---|
| sft_v1 | `train_sft.py --data data/train.jsonl --epochs 1.0 --lr 1.2e-5 --batch-size 2 --grad-accum 8`，block size 2048 | 从 `Qwen/Qwen3-4B-Base` 起的第一轮全参 SFT，43,687 条打成 10,448 个 block |
| sft_v2 | 从 sft_v1 续，`--sources opencode_verified,mbpp_train --mbpp-repeat 6 --epochs 0.4 --lr 3e-6` | 执行验证子集 + MBPP train，后面被高分数据路线盖过 |
| sft_v4 | 高分清洗子集（`open_high24k_clean3.jsonl`，`--open-count 24000 --magic-count 0 --require-high-judge --clean-targets`） | 高置信 OpenCodeInstruct |
| sft_v5 | 从 sft_v4 续：`--data data/open_high32k_clean_b.jsonl --epochs 1.0 --lr 4e-6 --save-steps 220 --batch-size 2 --grad-accum 8 --no-gradient-checkpointing` | 再 32k 高分样本，与前面几份互斥 |
| **交付** | `average_models.py runs/sft_v4_highclean runs/sft_v5_highclean_b/checkpoint-440 --alpha 0.625`，再 `cp -a` 进 `final_model` | 0.625·v4 + 0.375·v5@440 |

同时也插过 v1 与 v4（alpha 0.75）、v4 与 v5 的 0.6875 / 0.8125，留下的是 0.625 这一档。

### 2. 0.841 — claude-fable-5[1m]

`claude_non_api_max_claude-fable-5_1m__10h_run1__humaneval_Qwen_Qwen3-4B-Base_17337595`

官方 0.8415 ± 0.029。agent 在 epoch 1 结束（`work/run1/checkpoint-741`）后把该 checkpoint 拷进 `final_model` 当保险，随后 session limit，**2 epoch 没跑完**。官方分就是这个半成品。

计划中的混合（`work/prep_data.py`）：

```bash
python3 work/prep_data.py --out work/sft_mix_v1.jsonl \
  --n-evol 45000 --n-oss-py 22000 --n-self-oss 18000 \
  --n-kodcode 50000 --n-format 30000
```

对应 `ise-uiuc/Magicoder-Evol-Instruct-110K`、`ise-uiuc/Magicoder-OSS-Instruct-75K`（Python 子集）、`bigcode/self-oss-instruct-sc2-exec-filter-50k`、`KodCode/KodCode-V1-SFT-4o`，外加一份格式对齐数据。HumanEval 只作为去污染参照。

实际跑起来的训练：

```bash
python3 work/train_sft.py --data work/sft_mix_v1.jsonl --output work/run1 \
  --epochs 2 --per-device-bs 16 --grad-accum 2 --lr 1e-5
```

脚本默认基座 `Qwen/Qwen3-4B-Base`，`--max-length` 默认 4096。撞线时 loss 约 0.545→0.50，token accuracy 约 85%，step 时间约 9.1s。

---

## 这 14 条里反复出现的做法

- **先对齐评测渲染，再谈数据。** 几乎每条都把训练文本做成评测 template 的逐字节结果：AIME/GPQA/GSM8K 的 `ANSWER:` 收尾，BFCL 的单个 `<tool_call>`，写作和医疗的 chat 渲染。
- **`generation_config.json` 就是解码策略。** vLLM 忽略 `do_sample`，只读 `temperature` / `top_p` / `top_k` / `repetition_penalty`。GSM8K 上把温度写成 0.0 值 +7.5pt；写作上 repetition_penalty 1.05 值 +0.10；GPQA 上 repetition_penalty 1.06 把 77% 的截断打到 4.5%。
- **Qwen3 152k 词表的 logits 是单卡训练的硬约束。** 解法有三套：Liger / fused CE、只在监督位置上分块算 loss、8-bit Adam 加很小的 batch。
- **全参 SFT 是默认起点，RL 和 DPO 是加分项。** GSM8K 两条都上了 GRPO；写作和医疗的高分用了 on-policy DPO。GPQA 第二名是唯一一条 LoRA。
- **soup 经常被拿来收尾，但不是总赢。** AIME #1、GSM8K #1、HealthBench #1、HumanEval #1 的交付物都是权重平均；GPQA #1 的 soup 反而更差，没留。
