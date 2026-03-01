#!/usr/bin/env python3
"""
Backward-compatible wrapper.

The Sqorz ingester was renamed to `ingest_sqorz.py`.
This wrapper keeps old commands working.
"""

from ingest_sqorz import main


if __name__ == "__main__":
    main()
