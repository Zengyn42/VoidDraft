"""向后兼容 — schema 已迁移到 colony_coder/state.py.

旧代码 import 此模块时自动转发到新位置。
"""

# 转发所有符号到新位置
from blueprints.functional_graphs.colony_coder.state import (  # noqa: F401
    ColonyCoderState as ColonyCoderExecutorState,
)
from framework.schema.reducers import _merge_dict  # noqa: F401
