"""Deprecated filename; use codex-congress after installing the package."""

import sys
from codex_orchestrators import congress_engine as _engine

if __name__ == "__main__":
    _engine.main()
else:
    sys.modules[__name__] = _engine
