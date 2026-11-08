#!/usr/bin/env python3
# gfs_build.py — newest complete NOAA GFS run -> tiles, blocks, tail, manifest.
# Exit codes mirror hrrr_build.py: 0 built, 3 nothing to do (the live manifest
# already carries that run), 1 failed (the live site stays).
import sys

from globalfeed.cli import main
from globalfeed.models import GFS

if __name__ == "__main__":
    sys.exit(main(GFS))
