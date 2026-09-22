#!/usr/bin/env python3
"""PreToolUse 守卫：把 bench 规则和阶段纪律做成当场拦截，而不是事后 judge。

stdin 收 Claude Code 的 hook JSON，退出码 2 = 拦截（stderr 会回喂给模型）。
所有判定都追加到 research/guard.log，作为反作弊 judge 的审计证据。
失败一律放行（fail-open）：守卫本身不该成为流水线的单点故障。
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

RES = Path(os.environ.get("RESEARCH_DIR", "research"))
PHASE_FILE = RES / ".phase"
LOG = RES / "guard.log"

# 规则 4：评测协议本体不可改。research/ 下的这些是编排器的账本。
PROTECTED = [
    "evaluate.py", "timer.sh", "templates/",
    "research/state.json", "research/journal.md", "research/guard.log",
    "research/action_space.json", "research/step_policy.json",
    "research/settings.json", "research/guard.py",
    "research/harness_src",
]
# protocol.md 只允许协议 specialist / Step1 Measurement 阶段写，之后是既定事实
PHASE_WRITABLE = {"research/protocol.md": {"golden_protocol", "measurement"}}
EDIT_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit", "StrReplace"}
DESTRUCTIVE = re.compile(r"rm\s+(-[a-zA-Z]*\s+)*.*?(research/|final_model|best_model)")
# 规则 5：只精确拦截这三个全局搜索目标——`/`、`/root`、`/root/paddlejob/rl-public`，
# 会遍历整个文件系统或共享机上的他人目录。其它绝对路径（如 /root/x/y、/data/foo）不拦。
# 边界 (?:^|[\s;&|(]) 保证是命令段开头的 find；结尾 (?:\s|$|[;&|]) 保证 /root 不会
# 误匹配到 /root2 这类前缀相同的路径。
GLOBAL_FIND = re.compile(
    r"(?:^|[\s;&|(])find\s+(?:"
    r"/|/root/?|/root/paddlejob/rl-public/?"
    r")(?:\s|$|[;&|])"
)
# 规则 6：按名字/模式杀进程会误杀同机器上别人的进程。必须用具体数字 PID。
# 覆盖 pkill、killall，以及 kill 搭配 pgrep / `ps ... | grep` 这类字符串模式。
KILL_BY_NAME = re.compile(
    r"(?:^|[\s;&|(])(?:pkill|killall)\b"
    r"|(?:^|[\s;&|(])kill\b[^;&|]*\$(?:\()?(?:pgrep|pidof|ps\b)"
    r"|(?:^|[\s;&|(])kill\b[^;&|]*`[^`]*(?:pgrep|pidof|ps\b)[^`]*`"
    r"|(?:^|[\s;&|(])kill\b[^;&|]*\|\s*(?:xargs\s+)?kill\b"
    r"|(?:^|[\s;&|(])kill\b[^;&|]*\$\([^)]*\|[^)]*grep"
    r"|(?:^|[\s;&|(])xargs\s+(?:-[a-zA-Z0-9]+\s+)*kill\b"
)
# `kill 12345` / `kill -9 12345` 这类带数字 PID 的调用是放行的。
KILL_WITH_PID = re.compile(r"(?:^|[\s;&|(])kill\s+(?:-[a-zA-Z0-9]+\s+)*\d+(?:\s+\d+)*\s*(?:$|[;&|])")


def log(decision: str, tool: str, detail: str) -> None:
    try:
        RES.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"phase": phase(), "decision": decision,
                                "tool": tool, "detail": detail[:500]},
                               ensure_ascii=False) + "\n")
    except Exception:
        pass


def phase() -> str:
    # 并行节点各自从环境拿阶段；共享 .phase 仅用于旧版本兼容和人工查看。
    env_phase = os.environ.get("RESEARCH_PHASE")
    if env_phase:
        return env_phase
    try:
        return PHASE_FILE.read_text(encoding="utf-8").strip()
    except Exception:
        return "unknown"


def deny(tool: str, reason: str) -> None:
    log("deny", tool, reason)
    sys.stderr.write("BLOCKED by research harness guard: " + reason + "\n")
    sys.exit(2)


def hits_protected(raw: str) -> str | None:
    norm = raw.replace("./", "")
    for p in PROTECTED:
        if p in norm:
            return p
    for p, phases in PHASE_WRITABLE.items():
        if p in norm and phase() not in phases:
            return f"{p}（只在 {sorted(phases)} 阶段可写）"
    return None


def main() -> None:
    payload = json.load(sys.stdin)
    tool = payload.get("tool_name", "")
    ti = payload.get("tool_input", {}) or {}

    if tool in EDIT_TOOLS:
        target = str(ti.get("file_path") or ti.get("path") or ti.get("notebook_path") or "")
        hit = hits_protected(target)
        if hit:
            deny(tool, f"{target} 命中受保护路径 {hit}。evaluate.py/templates 由 bench 规则 4 锁定，"
                       "research/ 下的账本由编排器拥有；请把产物写到本节点目录。")

    if tool == "Bash":
        cmd = str(ti.get("command", ""))
        # 2>&1 / 1>&2 这类 fd 复用不是文件写入，先摘掉再判定（2026-09-08 用户决策：
        # 曾把 `bash timer.sh 2>&1 | head`、`python evaluate.py --help 2>&1` 这类纯读
        # 命令误判成"写受保护路径"，13/13 条 deny 都是这个形态）
        cmd = re.sub(r"\s\d*>&\d*", " ", cmd)
        if re.search(r"(>|>>|tee\s|sed\s+-i|truncate|cp\s|mv\s)", cmd):
            hit = hits_protected(cmd)
            if hit:
                deny(tool, f"命令试图写入受保护路径 {hit}：{cmd[:200]}")
        if DESTRUCTIVE.search(cmd):
            deny(tool, f"禁止删除研究状态/模型目录：{cmd[:200]}")
        # 规则 5：拦截对 / 、/root、/root/paddlejob/rl-public 的全局 find。
        # 逐段判断，命中即 deny。
        for seg in re.split(r"[;&|]+", cmd):
            if GLOBAL_FIND.search(" " + seg.strip()):
                deny(tool, f"禁止对 /、/root、/root/paddlejob/rl-public 做全局 find：{seg.strip()[:200]}。"
                           "请把查找范围限定在当前工作目录或具体子目录，如 `find ./research -name ...`。")
        # 规则 6：拦截按名字/模式杀进程，强制用具体数字 PID。
        if KILL_BY_NAME.search(cmd) and not KILL_WITH_PID.search(cmd):
            deny(tool, f"禁止按进程名/模式杀进程（可能误杀同机器他人进程）：{cmd[:200]}。"
                       "请先用 `pgrep -f <pattern>` 确认，再对具体数字 PID 执行 `kill <PID>`。")

    log("allow", tool, json.dumps(ti, ensure_ascii=False)[:300])
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:  # fail-open
        log("error", "?", repr(exc))
        sys.exit(0)
