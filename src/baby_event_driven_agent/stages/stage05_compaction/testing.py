"""[project.scripts] 入口：stage05-test。

pyproject 的 scripts 不能预设参数，所以测试任务在这里包一层 pytest.main。
（实现与 04 共享，tests 里的 import 指向 stage04_trajectory，见包 docstring。）
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


def main() -> None:
    tests_dir = Path(__file__).parent / "tests"
    code = pytest.main(["-q", str(tests_dir)])
    sys.exit(code)
