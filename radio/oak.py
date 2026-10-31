#!/usr/bin/env python3
"""
OAK area test: Oakland (OAK) and Hayward (HWD) tower / ground / clearance, plus the NorCal
sectors around them (125.35, 124.525, 124.8, 120.9, 135.1, ...).

Every transmission is saved (raw and cleaned) into recordings/oak_<time>/,
logged in transmissions.csv, and summarized at the end per channel.

    python3 radio/oak.py                  # 10 minutes
    python3 radio/oak.py --minutes 30 --listen
    python3 radio/oak.py --list           # channels and capture plan only
    python3 radio/oak.py --device ip:192.168.1.10   # over Ethernet
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))    # repo root, for atcsdr

from atcsdr.airband import area_main

if __name__ == "__main__":
    area_main("oak", default_minutes=10)
