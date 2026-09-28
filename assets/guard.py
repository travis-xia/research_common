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
import shlex
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


def _norm_path(raw: str) -> str:
    path = raw.strip().strip("'\"").replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    while "//" in path:
        path = path.replace("//", "/")
    return path.rstrip()


def _path_matches(candidate: str, protected: str) -> bool:
    """按路径边界匹配，避免 evaluate.py 命中 my_evaluate.py.bak。"""
    candidate = _norm_path(candidate)
    protected = _norm_path(protected)
    if not candidate or not protected:
        return False
    if protected.endswith("/"):
        directory = protected.rstrip("/")
        return candidate == directory or candidate.startswith(directory + "/")
    return (
        candidate == protected
        or candidate.endswith("/" + protected)
        or candidate.startswith(protected + "/")
    )


def hits_protected(raw: str) -> str | None:
    for p in PROTECTED:
        if _path_matches(raw, p):
            return p
    for p, phases in PHASE_WRITABLE.items():
        if _path_matches(raw, p) and phase() not in phases:
            return f"{p}（只在 {sorted(phases)} 阶段可写）"
    return None


def _split_clauses(command: str) -> list[str]:
    """按 shell 控制符拆命令，但不拆引号里的文本。"""
    clauses: list[str] = []
    current: list[str] = []
    in_single = in_double = False
    escaped = False
    i = 0
    while i < len(command):
        ch = command[i]
        if escaped:
            current.append(ch)
            escaped = False
            i += 1
            continue
        if ch == "\\" and (in_single or in_double):
            current.append(ch)
            escaped = True
            i += 1
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
            current.append(ch)
            i += 1
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            current.append(ch)
            i += 1
            continue
        if not in_single and not in_double:
            if ch in ";\n|":
                clauses.append("".join(current))
                current = []
                i += 1
                if ch in "&|" and i < len(command) and command[i] == ch:
                    i += 1
                continue
            if ch == "&" and i + 1 < len(command) and command[i + 1] == "&":
                clauses.append("".join(current))
                current = []
                i += 2
                continue
        current.append(ch)
        i += 1
    clauses.append("".join(current))
    return clauses


def _strip_fd_redirections(command: str) -> str:
    """去掉明显只是 fd 复用或丢弃输出的重定向。"""
    command = re.sub(r"(?<!\S)\d*>&\d+", " ", command)
    command = re.sub(r"(?<!\S)\d*>\s*/dev/(?:null|stdout|stderr|tty)\b", " ", command)
    return command


def _redirection_targets(command: str) -> list[str]:
    """只提取引号外真正的 > / >> / &> 文件目标。"""
    targets: list[str] = []
    in_single = in_double = False
    escaped = False
    i = 0
    while i < len(command):
        ch = command[i]
        if escaped:
            escaped = False
            i += 1
            continue
        if in_single:
            if ch == "'":
                in_single = False
            i += 1
            continue
        if in_double:
            if ch == "\\":
                escaped = True
            elif ch == '"':
                in_double = False
            i += 1
            continue
        if ch == "'":
            in_single = True
            i += 1
            continue
        if ch == '"':
            in_double = True
            i += 1
            continue
        if command.startswith("&>", i):
            i += 2
        elif ch == ">":
            # Python/文本里的 ->、=>、>=、>& 不是文件重定向。
            if i and command[i - 1] in "-=":
                i += 1
                continue
            if i + 1 < len(command) and command[i + 1] == "&":
                i += 2
                continue
            i += 2 if i + 1 < len(command) and command[i + 1] == ">" else 1
        else:
            i += 1
            continue
        while i < len(command) and command[i].isspace():
            i += 1
        start = i
        while i < len(command) and not command[i].isspace() and command[i] not in "|;&":
            i += 1
        if i > start:
            target = command[start:i].strip("'\"")
            if target not in {"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty"}:
                targets.append(target)
    return targets


def _write_targets(command: str) -> list[tuple[str, str]]:
    """返回 (目标路径, 操作类型)，只返回可能改变文件的目标。"""
    command = _strip_fd_redirections(command)
    targets = [(target, "redir") for target in _redirection_targets(command)]

    for clause in _split_clauses(command):
        try:
            tokens = shlex.split(clause)
        except ValueError:
            # shell 语法不完整时宁可放行，避免 guard 成为流水线单点故障。
            continue
        if not tokens:
            continue

        # 去掉前置环境变量赋值。
        while tokens and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", tokens[0]):
            tokens.pop(0)
        if not tokens:
            continue

        verb = tokens[0]
        args = [token for token in tokens[1:] if not token.startswith("-")]
        if verb == "tee":
            targets.extend((arg, "tee") for arg in args)
        elif verb == "cp" and args:
            targets.append((args[-1], "cp"))
        elif verb == "mv" and args:
            # mv 的源和目标都需要检查：移动 protected 文件本身也不应放行。
            targets.extend((arg, "mv") for arg in args)
        elif verb == "rm" and args:
            targets.extend((arg, "rm") for arg in args)
        elif verb == "truncate" and args:
            targets.extend((arg, "truncate") for arg in args if not arg.isdigit())
        elif verb == "sed" and any(arg == "-i" or arg.startswith("-i") for arg in tokens[1:]):
            # sed -i 's/old/new/' file：跳过选项和替换表达式，最后的文件是写目标。
            non_options = [
                arg for arg in tokens[1:]
                if not arg.startswith("-") and not re.match(r"^s[^\w].+", arg)
            ]
            targets.extend((arg, "sed") for arg in non_options)
    return targets


def _destructive_target(target: str) -> str | None:
    normalized = _norm_path(target)
    if normalized == "research" or normalized.startswith("research/"):
        return "research/"
    for name in ("final_model", "best_model"):
        if normalized == name or normalized.startswith(name + "/") or f"/{name}/" in normalized:
            return name
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
        for target, kind in _write_targets(cmd):
            hit = hits_protected(target)
            if hit:
                deny(tool, f"命令试图写入受保护路径 {hit}（{kind} -> {target}）：{cmd[:200]}")
            if kind in {"rm", "mv"}:
                destructive = _destructive_target(target)
                if destructive:
                    deny(tool, f"禁止删除/移动研究状态或模型目录 {destructive}：{cmd[:200]}")
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

    # 放行路径不写日志；guard.log 只保留实际拦截，避免高频只读命令造成大量 I/O。
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:  # fail-open
        # 异常也 fail-open，且不把正常/异常放行写入 guard.log；日志只记录 deny。
        sys.exit(0)
