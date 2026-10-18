"""墙钟预算。_DEADLINE 只在本模块维护，外部通过 budget._DEADLINE 读写。"""
from __future__ import annotations

import os
import re
import subprocess
import time

from .common import log
from .config import FINALIZE_RESERVE_MIN, TASK_DIR


_DEADLINE: float | None = None


def timer_remaining_h() -> float:
    timer = TASK_DIR / "timer.sh"
    if timer.is_file():
        try:
            output = subprocess.run(
                ["bash", str(timer)],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            ).stdout
            if "expired" in output.lower():
                return 0.0
            match = re.search(r"(\d+):(\d{2})", output)
            if match:
                return int(match.group(1)) + int(match.group(2)) / 60.0
        except Exception as exc:
            log(f"WARN 读取 timer.sh 失败: {exc!r}")
    try:
        return max(0.0, float(os.environ.get("NUM_HOURS", "10")))
    except ValueError:
        return 10.0


def remaining_h() -> float:
    if _DEADLINE is None:
        return timer_remaining_h()
    return max(0.0, (_DEADLINE - time.time()) / 3600.0)


def research_remaining_h() -> float:
    """研究节点可用时间：总剩余时间扣掉固定留给 finalizer 的部分。"""
    return max(0.0, remaining_h() - FINALIZE_RESERVE_MIN / 60.0)
