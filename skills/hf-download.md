# hf-download：HuggingFace 大数据集/模型下载（本机）

## 何时用（触发条件）

- 要**下载或 `load_dataset` 加载 >50MB 的外部数据集/模型**（OpenR1、NuminaMath、
  MetaMathQA 这个量级）时，先读完本文再动手。统一入口是同目录的 `hf-dl.sh`。
- 已在 `$HF_HOME/hub` 缓存里的 repo 不适用，直接加载即可（gsm8k/aime/gpqa 等已预铺）。

## 前因后果（2026-09-09 实录）

run `gsm8k_..._114749` 的 Golden Run：配方 pin 了 OpenR1-Math-220k + NuminaMath-1.5，
工程师节点用 `load_dataset`（python **单连接**）去拉——单连接过代理只有 ~1.4MB/s，
且单个 parquet 在**恰好 10 MiB（10485760 字节）处被掐断**。2-3GB 的数据集在 35 分钟
数据窗口内下不完，被迫 fallback 到本地缓存源，配方意图落空。

同一天用 `hfd.sh`（aria2c 多连接）实测**同一代理**：

| 方式 | 实测 |
| --- | --- |
| python 单连接（load_dataset / requests） | ~1.4 MB/s；单文件在恰好 10 MiB 处被掐 |
| hfd.sh（aria2c -x4 -j5，最多 20 连接） | OpenR1 全仓 12.65GB / 64s ≈ **197 MB/s**；NuminaMath-1.5 531MB / ~5s ≈ **100+ MB/s** |

差距两个数量级。**瓶颈不是带宽，是单连接姿势。**

## 本机网络事实

- 出外网必须走公司代理 `http://agent.baidu.com:8188`（http/https 同一地址）。
- `hf-mirror.com` 在本机会超时——不要设置 `HF_ENDPOINT`，统一走 `https://huggingface.co`。
- 代理对单连接限速 ~1.4MB/s 且 ~10MiB 处掐断；多连接聚合实测 100-200MB/s。
- `pip/uv` 装包、`git clone` 外网仓库同样需要上面的代理。

## 怎么做

```bash
bash <skills目录>/hf-dl.sh AI-MO/NuminaMath-1.5 --dataset    # 数据集
bash <skills目录>/hf-dl.sh open-r1/OpenR1-Math-220k --dataset --include data/*   # 只要某个 config
bash <skills目录>/hf-dl.sh <org/model>                        # 模型
```

- **断点续传**：Ctrl-C 中断后重跑同一命令即续传（hfd.sh 自带 manifest diff）。
- **复现**：实验要 pin 时透传 `--revision <commit-hash>`，并把 hash 记进产物 manifest。
- 下载落在**当前工作目录**的 `<repo-name>/` 子目录——先确认磁盘空间（gpfs 常年 93%）。

## 下载之后怎么用

下载结果是**平铺目录**（`<repo>/data/train-*.parquet` 等），不是 HF hub 缓存布局。
直接按 parquet 加载即可，不要试图手工改造缓存目录：

```python
from datasets import load_dataset
ds = load_dataset("parquet",
                  data_files="<repo>/data/train-*.parquet", split="train")
```

## 边界

- `<50MB` 的小数据集走正常 `load_dataset` 即可。
- 下载前先看 `$HF_HOME/hub` 是否已有（`datasets--org--name` 目录存在即命中缓存）。
