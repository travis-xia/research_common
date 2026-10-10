# 推理解码与停机符配置 (Inference & Decoding)

## 何时查阅
- 在检查或修改 `generation_config.json`、设置评测推理参数、或遇到模型胡言乱语、无限循环不停止时。

---

## 1. 停机符（EOS Tokens）显式双配置
- **现象与风险**：
  许多开源基座模型（如 Qwen 系列）默认配置仅将 `<|endoftext|>` 作为单一停机符。但在 Chat/Instruct 模板中，每个对话轮次通常以特殊 token（如 `<|im_end|>`）收尾。若未显式指定，推理引擎在遇到轮次结束符后不会停止，导致继续生成乱码甚至超时截断。
- **配置规则**：
  在模型保存目录下的 `generation_config.json` 或推理启动参数中，必须显式将模型相关的终止符作为一个列表写入：
  - 例如 Qwen 架构：`"eos_token_id": [151645, 151643]`
  - 具体取值应以 `research/protocol.md` 从源码读出的 Token ID 为准。

---

## 2. 确定性与解码参数显式声明
- **四个采样键必填**：
  在 `generation_config.json` 中，必须显式写全以下四个键，避免推理引擎按隐式默认值回退：
  ```json
  {
    "temperature": 0.0,
    "top_p": 1.0,
    "top_k": 50,
    "do_sample": false
  }
  ```
- **Temperature 取值原则**：
  - 短答案 / 严格格式任务：推荐贪心解码（`temperature: 0.0`, `do_sample: false`），可复现且格式稳定。
  - 长思维链 (Long CoT / `<think>` 标签) 任务：极端贪心解码可能导致陷入死循环无法闭合思维标签。若观察到严重截断或复读，改用轻微随机性；起点取目标基座同家族模型卡（Instruct / Thinking 版）对相应模式的官方推荐采样参数，再按 `recipe_guidelines.md` §4 在尺子子集上确认。
- **额外键同样写入文件**：如使用 `repetition_penalty` 等额外解码键，同样写进 `generation_config.json`。

### 推理引擎如何读取解码配置
- 部分推理引擎（如 vLLM）直接把 `generation_config.json` 中的采样键当作默认采样参数，并忽略 `do_sample`。只写 `do_sample: false` 不等于贪心，目标温度必须写进文件本身。
- 评测命令不传采样参数时，交付目录里的 `generation_config.json` 就是全部解码策略。本环境的 vLLM 只读取 `repetition_penalty`、`temperature`、`top_k`、`top_p`、`min_p`、`max_new_tokens` 这几个键（可在本机 vllm 源码 `config/model.py` 的 `get_diff_sampling_param` 核对）；`presence_penalty`、`frequency_penalty` 写进去不生效。模型卡推荐的 presence 类惩罚在本评测中无法通过交付目录生效，需要抑制复读时只能用 `repetition_penalty`。
- 两类惩罚尺度不同，数值不能照搬：`repetition_penalty` 是乘性系数，1.0 表示不惩罚，偏离 1 越远越强；`presence_penalty` 是加性项，0 表示不惩罚。
- 先查看基座自带 `generation_config.json` 的默认值，基座默认的采样参数通常不是评测想要的取值。
- 训练框架保存模型时可能用基座默认值覆盖 `generation_config.json`；保存后必须复核文件内容。

---

## 3. 生成上限取小原则
- 推理引擎最终的截断长度通常由模型的 `generation_config.max_new_tokens` 与评测请求传参中的上限**取较小值**决定。确保两者协调，切勿让模型自身的配置低于评测所需长度；基座默认的 `max_new_tokens` 常低于评测上限，需显式改成不小于评测上限。
