#!/usr/bin/env python3
# hf_probe.py —— 按名探测本地预铺目录是否已有某 HuggingFace repo（防重复下载）。
# 是 hf-dl.sh 的前置步骤：输出 1 直接用本地，输出 0 才下载。
#
# 设计约束（重要）：本工具只做「按名存在性探测」，刻意不提供任何枚举/列表
# 能力。预铺目录里有什么属于环境信息，不允许被「看见」——实验选型由任务
# 决定，不能被本地存量诱导。同理：不要 ls 该目录，只允许按名探测。
#
# 用法（一次只查一个）：
#   python3 hf_probe.py Qwen/Qwen3-4B       # 完整 org/name（匹配取尾段）
#   python3 hf_probe.py NuminaMath-1.5      # 裸名
#
# 输出（stdout，仅一行）：
#   1    可用：目录存在、非空、无 .aria2 残留
#   0    无可用：不存在 / 空目录 / 未下完
# 命中时的路径即 <POOL>/<repo名>/，与 hf-dl.sh 产物同构。
# 退出码恒 0（答案在 stdout），仅用法错误为 2。
import os
import sys
from pathlib import Path

POOL = Path(os.environ.get(
    "HF_PROBE_POOL",
    "/root/paddlejob/rl-public/xiasheng01/xs_workdir/hf_download",
))


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: hf_probe.py <org/repo|repo>", file=sys.stderr)
        return 2
    query = sys.argv[1]
    name = query.rsplit("/", 1)[-1]  # 去掉 org 前缀；平铺目录只按 repo 名存放
    if not name:
        print(f"[hf_probe] 非法 repo id: {query}", file=sys.stderr)
        return 2
    d = POOL / name
    usable = d.is_dir() and not any(d.rglob("*.aria2")) and any(d.iterdir())
    print(1 if usable else 0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
