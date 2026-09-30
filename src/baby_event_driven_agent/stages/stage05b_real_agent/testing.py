"""[project.scripts] 入口：stage05b-test。

pyproject 的 scripts 不能预设参数，所以测试任务在这里包一层 pytest.main。
（agent / 轨迹 / 压缩的实现与 04 共享；本目录 tests 只测 05b 的增量：
知识层、知识库后端的工具、真 UI 渲染路径。）
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


def main() -> None:
    tests_dir = Path(__file__).parent / "tests"
    code = pytest.main(["-q", str(tests_dir)])
    sys.exit(code)
