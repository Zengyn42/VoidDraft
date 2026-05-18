"""
VoidDraft test suite — pytest bootstrap.

Sets CWD to ZenithLoom (mirrors production: awaken.py does os.chdir there),
so relative paths like 'framework/nodes/tool_discovery' resolve correctly.

sys.path:
- ZenithLoom: engine (framework.*)
- VoidDraft:  blueprints (functional_graphs.*, role_agents.*)
"""
import sys
import os
from pathlib import Path

_TESTS      = Path(__file__).parent          # VoidDraft/tests/
_VOIDDRAFT  = _TESTS.parent                  # VoidDraft/
_FOUNDATION = _VOIDDRAFT.parent              # Foundation/
_ZENITHLOOM = _FOUNDATION / "ZenithLoom"

# Mirror production CWD (awaken.py does os.chdir(ZenithLoom))
os.chdir(str(_ZENITHLOOM))

sys.path.insert(0, str(_ZENITHLOOM))
sys.path.insert(0, str(_VOIDDRAFT))
