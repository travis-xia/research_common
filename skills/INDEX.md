# RSI Skills 技能索引手册

本文档为当前环境下的工程指南与领域先验索引。Agent 在进行相关决策、检查或实验实现时，应根据当前任务需要自主读取对应的指南。

---

## 1. 模型后训练 (Post-Training) 指南
- **[训练方案与超参指南](post_train/recipe_guidelines.md)** (`skills/post_train/recipe_guidelines.md`)
  - *触发时机*：制定训练配方、调整学习率、批次大小、Epoch、LoRA vs 全参选择时。
  - *核心内容*：收敛性原则、学习率安全区间、有效批次大小设置、防止过拟合的经验纪律。

- **[推理解码与停机符配置](post_train/inference_and_decoding.md)** (`skills/post_train/inference_and_decoding.md`)
  - *触发时机*：遇到输出截断、重复无意义字符、模型不停止回答，或调整 generation_config 时。
  - *核心内容*：双 EOS 停机符显式配置、四个采样键完整性、Temperature 针对长短任务的取值差异。

- **[评测对齐与防污染规范](post_train/eval_alignment.md)** (`skills/post_train/eval_alignment.md`)
  - *触发时机*：设计训练数据格式、对齐提示词模板、执行数据防污染自查时。
  - *核心内容*：评测输入输出四层对齐（上下文、包装、正文、收尾）、答案格式严格对应、测试集污染自查。

---

## 2. 工程部署与基础设施 (Engineering)
- **[HuggingFace 高速多连接下载](engineering/hf_download.md)** (`skills/engineering/hf_download.md`)
  - *触发时机*：需要从 HuggingFace 拉取大于 50MB 的大型数据集或权重时。
  - *核心内容*：本地网络代理配置、aria2c 多连接加速脚本 `hf-dl.sh` 的使用方法及规避 10MiB 断流。

- **[模型交付与 vLLM 起服检查](engineering/vllm_serving.md)** (`skills/engineering/vllm_serving.md`)
  - *触发时机*：在 Engineer_2 开工前检查或 Engineer_1 训练完成准备评测/交付时。
  - *核心内容*：权重目录完整性清单（config, safetensors, tokenizer, 禁止 adapter 残留）、vLLM 启动兼容性自检。

- **[运行前代码与工程核验规范](engineering/preflight_engineering_check.md)** (`skills/engineering/preflight_engineering_check.md`)
  - *触发时机*：在 Engineer_1 完成代码实现后、真正启动占卡运行之前，由 Engineer_2 进行审查时。
  - *核心内容*：Prompt 模板协议一致性、双 EOS/解码采样配置、超参笔误自查、语法与路径合法性、就地修改纪律。
