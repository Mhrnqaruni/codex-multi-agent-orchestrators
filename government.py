"""Compatibility entry point; use codex-government after installation."""

import sys
from codex_orchestrators import government_engine as _engine

if __name__ == "__main__":
    _engine.main()
else:
    sys.modules[__name__] = _engine
