#!/usr/bin/env python3
"""
WeatherBet Full — 20 cities, Kelly Criterion, stops, self-calibration
DigitalOcean App Platform ready
"""
import json
import time
import os
import requests
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from collections import defaultdict
import logging

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

@dataclass
class City:
    name: str
    station: str
    lat: float
    lon: float

# 20 Cities (Polymarket resolution airports)
CITIES = [
    City("Chicago", "KORD", 41.97, -87.90),
    City("NYC", "KLGA", 40.78, -73.88),
    City("Dallas", "KDAL", 32.85, -96.85),
    City("Miami", "KMIA", 25.79, -80.29),
    City("Seattle", "KSEA", 47.45, -122.31),
    City("Atlanta", "KATL", 33.64, -84.43),
    City("LA", "KLAX", 33.94, -118.41),
    City("Phoenix", "KPHX", 33.43, -112.01),
    City("Denver", "KDEN", 39.86, -104.67),
    City("Boston", "KBOS", 42.37, -71.01),
    City("London", "EGLC", 51.51, -0.07),
    City("Tokyo", "RJTT", 35.55, 139.78),
    City("Sydney", "YSSY", -33.95, 151.18),
    City("SaoPaulo", "SBSP", -23.43, -46.47),
    City("Toronto", "CYYZ", 43.68, -79.63),
    City("MexicoCity", "MMMX", 19.44, -99.08),
    City("Paris", "LFPG", 49.01, 2.55),
    City("Berlin", "EDDB", 52.37, 13.50),
    City("Mumbai", "VABB", 19.09, 72.87),
    City("Singapore", "WSSS", 1.36, 103.99)
]

class Config:
    def __init__(self):
        self.balance = float(os.environ.get("BALANCE", "10000.0"))
        self.max_bet = float(os.environ.get("MAX_BET", "20.0"))
        self.min_ev = float(os.environ.get("MIN_EV", "0.05"))
        self.max_price = float(os.environ.get("MAX_PRICE", "0.45"))
        self.min_volume = int(os.environ.get("MIN_VOLUME", "2000"))
        self.min_hours = float(os.environ.get("MIN_HOURS", "2.0"))
        self.max_hours = float(os.environ.get("MAX_HOURS", "72.0"))
        self.kelly_fraction = float(os.environ.get("KELLY_FRACTION", "0.25
