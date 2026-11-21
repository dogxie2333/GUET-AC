"""pytest 默认发现入口：真正的用例在任务指定的 stage_web_guet.test.py。

文件名带点无法被 pytest 默认的 test_*.py 规则发现，也无法用默认 import 模式导入；
这里按路径加载并把 test_* 函数导出到本模块，使 `pytest` / `pytest tests/web` 都能收集。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SOURCE = Path(__file__).with_name("stage_web_guet.test.py")
_SPEC = importlib.util.spec_from_file_location("stage_web_guet_dotted_tests", _SOURCE)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)

for _name in dir(_MODULE):
    if _name.startswith("test_"):
        _candidate = getattr(_MODULE, _name)
        if callable(_candidate):
            globals()[_name] = _candidate
