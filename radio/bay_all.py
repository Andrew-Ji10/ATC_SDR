#!/usr/bin/env python3
"""
Whole-Bay survey: Every channel in the South Bay, SFO and OAK sets at once, plus guard
(121.5): 43 channels, 118.0-135.65 MHz. Over USB the Pluto rotates through
3 capture windows; over Ethernet (--device ip:...) it hears them all at once.
A quick way to see what this antenna and location can pick up.

Every transmission is saved (raw and cleaned) into recordings/bay_<time>/,
logged in transmissions.csv, and summarized at the end per channel.

    python3 radio/bay_all.py                  # 10 minutes
    python3 radio/bay_all.py --minutes 30 --listen
    python3 radio/bay_all.py --list           # channels and capture plan only
    python3 radio/bay_all.py --device ip:192.168.1.10   # over Ethernet
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))    # repo root, for atcsdr

from atcsdr.airband import area_main

if __name__ == "__main__":
    area_main("bay", default_minutes=10)
