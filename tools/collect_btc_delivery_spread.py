#!/usr/bin/env python3
"""Compatibility entry point; now discovers all inverse delivery assets and expiries."""
try:
    from .collect_delivery_spread import main
except ImportError:
    from collect_delivery_spread import main

if __name__ == "__main__":
    raise SystemExit(main())
