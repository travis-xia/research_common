# 训练方案与超参指南 (Recipe Guidelines)

## 何时查阅
- 在 Step0 汇总输出主干配方，或 Step2/3 构思训练相关的猜想与实验方案时。

---

## 1. 训练范式选择
- **全参微调 vs LoRA/QLoRA**：
  - 若目标涉及“改变输出协议（如停止符行为、回答结构、思考标签格式）”或“深度任务推理能力提升”，优先采用全参微调（Full-parameter fine-tuning, bf16）。LoRA 常因未能充分调整词表/输出层而在协议对齐上失效。
  - 若显存受限或仅在极窄分布上做轻量模式注入，可使用 LoRA，但需密切监控格式遵循情况。
- **单阶段 vs 多阶段**：
  - 优先以稳定、高质量的 SFT 建立基准主干。
  - 在奖励函数高度明确、可程序化自动化验证（如数学终答、单元测试通过率）时，完全可以考虑引入 RL（比如GRPO） 阶段，使得模型收敛到正确分布。

---

## 2. 常用超参基准范围（经验先验）
- **学习率 (Learning Rate)**：
  - 全参 SFT：通常推荐在 `1e-5` 到 `2e-5` 之间，配合 Cosine 调度器与 3% Warmup。
  - LoRA：通常在 `1e-4` 到 `2e-4` 之间。
  - RL 阶段：学习率通常比 SFT 低一个数量级（`1e-6` 到 `3e-6`）。
- **训练轮数 (Epochs)**：
  - 绝大多数特定领域微调推荐 1 ~ 2 个 Epoch。微调超过 3 个 Epoch 极易发生过拟合及通用能力退化。
- **批次大小与吞吐优化（先大后小、爆显存再减半）**：
  - 有效 Batch Size 建议保持在 16 ~ 64。
  - **per-device bs 先大后小**：effective batch 定了之后，单卡批次（per-device bs）往大试（如从 8 或 16 起步），**真实爆显存（OOM）再减半**并成倍增加梯度累积步数（gradient_accumulation_steps）以补齐有效 batch。
  - 严禁凭空盲目把 bs 压得极小（吞吐大幅下降白白烧墙钟）。
  - 尽可能启用 FlashAttention-2 与 Gradient Checkpointing，词表大时使用 fused/chunked cross entropy 避免 logits OOM。
- **序列长度**：
  - `max_seq_len` 应根据目标任务协议的实际输入长度与最大生成上限总和反推，避免无意义的过大 padding 或粗暴截断。
