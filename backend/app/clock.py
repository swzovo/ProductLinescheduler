from __future__ import annotations

import os
from datetime import date


def generation_today() -> date:
    """排产边界使用本机当天；自动化测试可用环境变量固定日期。"""

    override = os.environ.get("SCHEDULER_TODAY")
    return date.fromisoformat(override) if override else date.today()
