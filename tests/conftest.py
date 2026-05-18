"""
VoidDraft test suite — pytest bootstrap.
- ZenithLoom: engine (framework.*)
- VoidDraft:  blueprints (functional_graphs.*, role_agents.*)
"""
import sys
from pathlib import Path

_HERE = Path(__file__).parent        # VoidDraft/tests/
_VOIDDRAFT = _HERE.parent            # VoidDraft/
_ZENITHLOOM = _VOIDDRAFT.parent / "ZenithLoom"

sys.path.insert(0, str(_ZENITHLOOM))
sys.path.insert(0, str(_VOIDDRAFT))
