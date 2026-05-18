from typing import get_type_hints, get_args
from typing import Annotated


def test_merge_dict_basic():
    from framework.schema.reducers import _merge_dict
    a = {"claude_main": "uuid1", "shared": "old"}
    b = {"gemini_main": "uuid2", "shared": "new"}
    assert _merge_dict(a, b) == {"claude_main": "uuid1", "gemini_main": "uuid2", "shared": "new"}


def test_merge_dict_empty():
    from framework.schema.reducers import _merge_dict
    assert _merge_dict({}, {"k": "v"}) == {"k": "v"}
    assert _merge_dict({"k": "v"}, {}) == {"k": "v"}


def test_base_state_node_sessions_has_merge_reducer():
    from framework.schema.base import BaseAgentState
    from framework.schema.reducers import _merge_dict
    hints = get_type_hints(BaseAgentState, include_extras=True)
    ann = hints["node_sessions"]
    args = get_args(ann)
    assert args[1] is _merge_dict, "node_sessions must have _merge_dict reducer"


def test_debate_state_node_sessions_has_merge_reducer():
    from framework.schema.debate import DebateState
    from framework.schema.reducers import _merge_dict
    hints = get_type_hints(DebateState, include_extras=True)
    ann = hints["node_sessions"]
    args = get_args(ann)
    assert args[1] is _merge_dict


def test_colony_coder_state_node_sessions_has_merge_reducer():
    from blueprints.functional_graphs.colony_coder.state import ColonyCoderState
    from framework.schema.reducers import _merge_dict
    hints = get_type_hints(ColonyCoderState, include_extras=True)
    ann = hints["node_sessions"]
    args = get_args(ann)
    assert args[1] is _merge_dict
