#!/usr/bin/env python3
# hf_probe.py —— 按名探测本地预铺目录是否已有某 HuggingFace repo（防重复下载）。
# 是 hf-dl.sh 的前置步骤：有可用命中直接用本地，没有命中才下载。
#
# 设计约束（重要）：本工具只做「按名存在性探测」，刻意不提供任何枚举/列表
# 能力。预铺目录里有什么属于环境信息，不允许被「看见」——实验选型由任务
# 决定，不能被本地存量诱导。同理：不要 ls 该目录，只允许按名探测。
#
# 匹配规则：对查询名与预铺目录下每个子目录名做「双向子串」匹配——查询名
# 是目录名的子串，或目录名是查询名的子串，均算命中，便于带出命名相关的
# 其他变体目录。
#
# 用法（一次只查一个）：
#   python3 hf_probe.py Qwen/Qwen3-4B       # 完整 org/name（匹配取尾段）
#   python3 hf_probe.py NuminaMath-1.5      # 裸名
#
# 输出（stdout，纯文字，供人/agent 读，没有固定格式协议）：
#   命中：列出每个命中目录名、是否可用（非空且无 .aria2 残留）、完整路径。
#   未命中：明确说明没有找到，提示走下载流程。
# 退出码恒 0（结果在 stdout），仅用法错误为 2。
import os
import sys
from pathlib import Path

POOL = Path(os.environ.get(
    "HF_PROBE_POOL",
    "/root/paddlejob/rl-public/xiasheng01/xs_workdir/hf_download",
))


def is_usable(d: Path) -> bool:
    """目录存在、非空、且没有 .aria2 残留（未下完的标记）才算可用。"""
    return d.is_dir() and not any(d.rglob("*.aria2")) and any(d.iterdir())


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: hf_probe.py <org/repo|repo>", file=sys.stderr)
        return 2
    query = sys.argv[1]
    name = query.rsplit("/", 1)[-1]  # 去掉 org 前缀；平铺目录只按 repo 名存放
    if not name:
        print(f"[hf_probe] 非法 repo id: {query}", file=sys.stderr)
        return 2

    if not POOL.is_dir():
        print(f"[hf_probe] 预铺目录不存在: {POOL}，视为无可用，请走下载流程。")
        return 0

    name_lower = name.lower()
    hits = []
    for d in POOL.iterdir():
        if not d.is_dir():
            continue
        dname_lower = d.name.lower()
        # 双向子串匹配：查询名是目录名子串，或目录名是查询名子串
        if name_lower in dname_lower or dname_lower in name_lower:
            hits.append(d)

    if not hits:
        print(f"[hf_probe] 查询: {query}（匹配名: {name}）")
        print(f"[hf_probe] 在预铺目录 {POOL} 下未找到任何名称匹配的子目录。")
        print("[hf_probe] 结论: 无可用，请走 hf-dl.sh 下载。")
        return 0

    print(f"[hf_probe] 查询: {query}（匹配名: {name}）")
    print(f"[hf_probe] 在预铺目录 {POOL} 下找到 {len(hits)} 个名称匹配的子目录：")
    any_usable = False
    for d in sorted(hits, key=lambda p: p.name.lower()):
        usable = is_usable(d)
        any_usable = any_usable or usable
        if usable:
            status = "可用（非空、无 .aria2 残留）"
        elif not any(d.iterdir()):
            status = "不可用（空目录）"
        elif any(d.rglob("*.aria2")):
            status = "不可用（存在 .aria2 残留，下载未完成）"
        else:
            status = "不可用"
        print(f"  - 目录名: {d.name}  状态: {status}  路径: {d}")

    if any_usable:
        print("[hf_probe] 结论: 存在可用命中，请从上面可用的目录中按需选用，"
              "原地引用、不要复制或移动")
    else:
        print("[hf_probe] 结论: 命中的目录均不可用（空或未下完），请走 hf-dl.sh 下载。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
