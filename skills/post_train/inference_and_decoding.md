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
  - 长思维链 (Long CoT / `<think>` 标签) 任务：极端贪心解码（temp=0）可能导致陷入死循环无法闭合思维标签。若观察到严重截断或复读，可尝试使用轻微随机性（如 `temperature: 0.6`, `top_p: 0.95`）。

---

## 3. 生成上限取小原则
- 推理引擎最终的截断长度通常由模型的 `generation_config.max_new_tokens` 与评测请求传参中的上限**取较小值**决定。确保两者协调，切勿让模型自身的配置低于评测所需长度。
