# 模型交付与 vLLM 起服检查 (Model Delivery & vLLM Serving)

## 何时查阅
- 在开工前检查 (Preflight) 以及训练完成、即将执行官方评测或最终交付时。

---

## 1. 交付模型目录完整性清单
模型目录必须是一个自包含、可直接被 Hugging Face / vLLM 独立加载的合法目录。缺失任何关键文件都会导致起服报错：
1. **配置文件**：`config.json`, `generation_config.json`（必须存在且合法）。
2. **权重分片与索引**：
   - 包含完整的 `model-*.safetensors`。
   - 若存在分片索引 `model.safetensors.index.json`，文件中声明的所有分片必须真实存在于同目录下。
3. **Tokenizer 配置文件**：
   - `tokenizer.json`, `tokenizer_config.json`, `vocab.json`（或对应分词器文件）。
   - `chat_template.json`（若包含模板，确保 Jinja 语法合法未被破坏）。
4. **禁止残留 LoRA 标记**：
   - 全参模型目录下**严禁**残留 `adapter_config.json` 或 `adapter_model.safetensors`，否则 Hugging Face 会误判为 LoRA 结构导致加载失败。

---

## 2. vLLM 起服常见坑与自检
- **起服快速探针**：
  在耗费大量时间进行全量评测前，可在后台启动轻量探测或通过 python 代码尝试加载：
  ```python
  from transformers import AutoConfig, AutoTokenizer
  config = AutoConfig.from_pretrained("./model_dir")
  tokenizer = AutoTokenizer.from_pretrained("./model_dir")
  ```
- **端口与显存管理**：
  确保先前的推理或评测进程已正常退出，未残留孤儿进程霸占显存。
