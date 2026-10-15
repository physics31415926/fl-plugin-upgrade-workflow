"""Compatibility entry point; implementation lives in fl_dashboard.services.share_server."""
import sys
from importlib import import_module

implementation = import_module("fl_dashboard.services.share_server")
if __name__ == "__main__":
    raise SystemExit(implementation.main())
sys.modules[__name__] = implementation
globals().update({key: value for key, value in vars(implementation).items() if not key.startswith("__")})
