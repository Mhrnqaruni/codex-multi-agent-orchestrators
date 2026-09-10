"""Compatibility entry point; Congress v2 is the single canonical engine."""

import sys
import congress2 as _engine

if __name__ == "__main__":
    _engine.main()
else:
    sys.modules[__name__] = _engine
