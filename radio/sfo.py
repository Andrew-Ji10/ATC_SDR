#!/usr/bin/env python3
"""
SFO area test: SFO and San Carlos (SQL) tower / ground / clearance, plus the NorCal
sectors for SFO arrivals and departures (128.325, 128.575, 120.9, 135.1, ...).

Every transmission is saved (raw and cleaned) into recordings/sfo_<time>/,
logged in transmissions.csv, and summarized at the end per channel.

    python3 radio/sfo.py                  # 10 minutes
    python3 radio/sfo.py --minutes 30 --listen
    python3 radio/sfo.py --list           # channels and capture plan only
    python3 radio/sfo.py --device ip:192.168.1.10   # over Ethernet
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))    # repo root, for atcsdr

from atcsdr.airband import area_main

if __name__ == "__main__":
    area_main("sfo", default_minutes=10)
