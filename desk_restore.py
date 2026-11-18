#!/usr/bin/env python3
"""desk_restore.py — lesstools LT-1 restore CLI (staging default; the
live desk is never touched without --in-place and the designer go). See
desk_snapshots.py."""
from desk_snapshots import main

if __name__ == "__main__":
    main()
