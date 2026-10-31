#!/usr/bin/env python3
"""
South Bay test: SJC, PAO, RHV and NUQ tower / ground / clearance, plus the NorCal
approach and departure sectors around them (SJC App 120.1, SJC Dep 121.3, ...).

Every transmission is saved (raw and cleaned) into recordings/south_bay_<time>/,
logged in transmissions.csv, and summarized at the end per channel.

    python3 radio/south_bay.py                  # 10 minutes
    python3 radio/south_bay.py --minutes 30 --listen
    python3 radio/south_bay.py --list           # channels and capture plan only
    python3 radio/south_bay.py --device ip:192.168.1.10   # over Ethernet
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))    # repo root, for atcsdr

from atcsdr.airband import area_main

if __name__ == "__main__":
    area_main("south_bay", default_minutes=10)
