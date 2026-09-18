# 运行前代码与工程检查规范 (Preflight Engineering Check Skill)

## 1. 核心目标与定位
在 Engineer_1 完成训练/评测代码编写后、**在真正启动占卡运行之前**，由审查工程师结合「科学猜想假说 + 规划方案 + 实际代码实现」进行静态审查与逻辑核验。
**核心目的**：单纯排除代码语法、超参笔误、协议格式错配、配置遗漏等低级工程问题，避免工程瑕疵损害科学实验的真实效果。

---

## 2. 工程核验细节清单 (Preflight Checklist)

### A. 协议对齐与格式核验 (Protocol & Format Alignment)
- [ ] **Prompt 与模板一致性**：检查代码中生成或处理数据的 prompt 模板、system prompt、引导词，是否与 `research/protocol.md` 定义的官方格式逐字对齐。
- [ ] **答案标记匹配**：检查训练数据答案包裹标识与评测答案抽取正则是否一致（例如：官方评测要求 `####` 还是 `ANSWER:`，严禁错位）。
- [ ] **终止符 (Stop Token / EOS)**：检查生成配置或 `generation_config.json` 是否显式配全全部必要终止符（如 Qwen3 系列必须包含 `[151643, 151645]`），防止评测端服务端出现大面积截断、超时或多轮死循环。
- [ ] **采样四键显式全量**：检查 `generation_config.json` 或起服/推理参数，`temperature`, `top_p`, `top_k`, `do_sample` 是否显式写全（避免因缺少 key 导致推理引擎回退到默认采样 `temperature=1.0` 吃掉正确率）。

### B. 脚本实现与超参核验 (Script Implementation & Hyperparameters)
- [ ] **超参笔误自查**：核对代码或启动参数中的学习率（LR）、Batch Size、Epochs、Warmup Steps 是否与方案严格一致，特别防范数量级笔误（如把 `2e-5` 误写为 `2e-4` 或 `2e-6`）。
- [ ] **控制变量受控**：确认除方案声明的修改变量外，其他训练超参与骨干配置保持严格受控不变，严禁代码中顺带改动未声明的参数。
- [ ] **数据处理逻辑**：检查数据分词、截断逻辑（`max_length` / `max_tokens`），确保输入截断不会切断题干末尾或答案区域。
- [ ] **环境与显存控制（bs先大后小，OOM再减半）**：检查 batch size、梯度累积步数、序列长度配置。坚持 per-device batch size 先大后小，只有发生真实 OOM 时才减半并翻倍 gradient_accumulation_steps 补齐有效批次；严禁凭空盲目将 batch size 压得很小导致吞吐腰斩。开启 FlashAttention-2 与 Gradient Checkpointing。

### C. 代码健康与交付自包含 (Code Health & Deliverables)
- [ ] **语法与路径合法性**：静态检查新增的 Python 脚本或 shell 命令，确认无语法错误、未导入的依赖包或不存在的路径引用。
- [ ] **调试硬编码消除**：确保没有残留调试阶段的断点、硬编码的小规模截断（如 `dataset[:5]`）或死循环逻辑。
- [ ] **目录与产物覆盖防护**：检查输出保存路径（Save Path），确保独立自包含，严禁覆盖 Baseline 或上一轮最佳 Checkpoint。

---

## 3. 处理纪律
1. **就地顺手修掉 (In-place Fix)**：
   发现上述清单中的任何问题，直接使用文件编辑工具修改盘上代码或配置文件，不要只提建议。
2. **严守科学语义**：
   只修复工程实现瑕疵与参数笔误，严禁擅自修改研究方案的核心假说、改动方法论或篡改评测基准。
3. **沉淀修复报告**：
   在检查报告中清晰记录修改的文件、修复前后的具体差异以及原因。
