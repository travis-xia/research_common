#!/usr/bin/env bash
# hf-dl.sh —— 本机下载 HuggingFace 数据集/模型的唯一正确姿势。
# 前因后果与实测数字见同目录 hf-download.md；prompt 里"下载 >50MB 外部数据集前必读"
# 指的就是那篇。代理/端点已在此配好，调用方不需要再设任何环境变量。
#
# 用法：
#   bash hf-dl.sh <org/repo> --dataset          # 数据集
#   bash hf-dl.sh <org/repo>                    # 模型
#   其余 hfd.sh 参数原样透传：--revision <hash>（复现要 pin）、--include/--exclude、-x/-j
#   例：bash hf-dl.sh AI-MO/NuminaMath-1.5 --dataset
#
# 下载落在当前工作目录的 <repo-name>/ 子目录（断点续传：中断后重跑同一命令即续传）。
set -euo pipefail

# 本机出外网必须走公司代理；单连接会被掐在 ~10MiB，hfd.sh 用 aria2c 多连接聚合
export http_proxy="${http_proxy:-http://agent.baidu.com:8188}"
export https_proxy="${https_proxy:-http://agent.baidu.com:8188}"
# hf-mirror.com 在本机会超时，统一走 huggingface.co
unset HF_ENDPOINT

command -v aria2c >/dev/null || { echo "[hf-dl] 缺少 aria2c" >&2; exit 1; }

HFD="${HFD:-/root/ComateProjects/chats/hf_download/hfd.sh}"
if [[ ! -f "$HFD" ]]; then
    # 布局兜底：skills 目录在 <chats>/myptbench/PostTrainBench/agents/research_common/skills
    HFD="$(cd "$(dirname "$0")" && pwd)/../../../../../hf_download/hfd.sh"
fi
[[ -f "$HFD" ]] || { echo "[hf-dl] 找不到 hfd.sh（可用 HFD=... 显式指定）" >&2; exit 1; }

echo "[hf-dl] 下载将落在 $PWD/<repo-name>/，注意磁盘空间；Ctrl-C 中断后重跑即续传" >&2
exec bash "$HFD" "$@"
