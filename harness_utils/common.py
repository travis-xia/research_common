"""日志、文件读写与节点目录分配。"""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from .config import NODES


_NODE_LOCK = threading.Lock()


def log(message: str) -> None:
    print(f"[orch {datetime.now().strftime('%H:%M:%S')}] {message}",
          flush=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError, RecursionError):
        return default


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(path)


def read_text(path: Path, default: str = "") -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, OSError):
        return default


def next_node_id(kind: str) -> str:
    """节点编号只用于避免覆盖文件，不承担研究状态含义。"""
    with _NODE_LOCK:
        maximum = -1
        if NODES.is_dir():
            for item in NODES.iterdir():
                match = re.match(r"^n(\d+)-", item.name)
                if match:
                    maximum = max(maximum, int(match.group(1)))
        return f"n{maximum + 1:03d}-{kind}"


def node_dir(kind: str) -> Path:
    with _NODE_LOCK:
        maximum = -1
        if NODES.is_dir():
            for item in NODES.iterdir():
                match = re.match(r"^n(\d+)-", item.name)
                if match:
                    maximum = max(maximum, int(match.group(1)))
        path = NODES / f"n{maximum + 1:03d}-{kind}"
        path.mkdir(parents=True, exist_ok=False)
        return path
