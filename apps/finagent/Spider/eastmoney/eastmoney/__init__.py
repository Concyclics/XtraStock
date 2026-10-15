# -*- coding: utf-8 -*-
"""导入本包时把项目根目录加入 sys.path，使 `from config import ...` 可用。"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
