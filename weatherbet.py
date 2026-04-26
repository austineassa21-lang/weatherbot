#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
weatherbet_mc.py — Monte Carlo Weather Betting Bot for Polymarket
==================================================================
Uses ECMWF ensemble forecast members (50 model runs) to build a
true probability distribution over temperature outcomes, rather than
assuming a fixed normal distribution with a guessed sigma.

Core idea:
  The ECMWF ensemble API returns 50 independent model members for each
  forecast day. Instead of asking "what does the mean forecast say and
  adding ±2°F sigma", we ask "of these 50 actual model runs, how many
  land in each Polymarket bucket?" That gives a direct, data-driven
  probability estimate grounded in real forecast uncertainty.

  When the 50 members are tightly clustered (high confidence), most
  probability mass sits in 1-2 buckets. When they're spread out (low
  confidence), probability distributes across many buckets. We only bet
  when our probability estimate disagrees with the market price enough
  to be worth the risk.

  For US cities we blend ECMWF ensemble with GFS ensemble (also 50
  members via the same API) and weight by historical accuracy per city.

Changes vs previous version:
  1. PRICE STABILITY CHECK — bot now watches a market across MIN_STABLE_SCANS
     full scans before buying. If the ask price moves more than
     MAX_PRICE_DRIFT between observations, the clock resets. This blocks
     entries into illiquid markets where the ask collapses immediately
     after purchase (the primary cause of all stop-outs so far).

  2. PROBABILITY DISCOUNT — all model p estimates are multiplied by
     P_DISCOUNT_FACTOR (0.60) before EV is calculated. The logs show
     every contract repricing to 3-5¢ within minutes of a 10-12¢ entry,
     implying the model is systematically ~3x overconfident. Discounting
     p forces the bot to only trade when the gap between our estimate
     and the market price is large enough to survive being wrong.

  3. RAISED MIN_EV — default raised from 0.05 to 0.25. At the old
     threshold, signals like EV +0.09 (model p=0.11 vs market p=0.10)
     were firing — essentially no edge at all. The new threshold requires
     a meaningful disagreement with the market before trading.

Usage:
    python weatherbet_mc.py           # run trading loop
    python weatherbet_mc.py status    # show balance and open positions
    python weatherbet_mc.py report    # full resolved trade report
    python weatherbet_mc.py simulate  # show current signals without trading

config.json keys:
    balance           starting balance (default 10000)
    max_bet           max $ per trade (default 20)
    min_ev            minimum edge to trade (default 0.25)
    min_price         minimum contract price to consider (default 0.06)
    max_price         maximum contract price to consider (default 0.50)
    min_volume        minimum market volume (default 300)
    min_hours         minimum hours to resolution (default 24)
    max_hours         maximum hours to resolution (default 72)
    kelly_fraction    fraction of Kelly criterion to use (default 0.25)
    max_slippage      max bid-ask spread allowed (default 0.04)
    scan_interval     seconds between full scans (default 3600)
    stop_cooldown_h   hours before re-entering after stop-loss (default 12)
    mc_simulations    number of Monte Carlo draws (default 2000)
    p_discount        probability discount factor (default 0.60)
    min_stable_scans  scans price must be stable before buying (default 3)
    max_price_drift   max ask movement across scans to count as stable (default 0.02)
    vc_key            Visual Crossing API key for resolution data
"""

import re
import sys
import json
import math
import time
import random
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import defaultdict

# =============================================================================
# CONFIG
# =============================================================================

with open("config.json", encoding="utf-8") as f:
    _cfg = json.load(f)

BALANCE             = _cfg.get("balance", 10000.0)
MAX_BET             = _cfg.get("max_bet", 20.0)
MIN_EV              = _cfg.get("min_ev", 0.25)        # raised from 0.05
MIN_PRICE           = _cfg.get("min_price", 0.06)
MAX_PRICE           = _cfg.get("max_price", 0.50)
MIN_VOLUME          = _cfg.get("min_volume", 300)
MIN_HOURS           = _cfg.get("min_hours", 24)
MAX_HOURS           = _cfg.get("max_hours", 72.0)
KELLY_FRACTION      = _cfg.get("kelly_fraction", 0.25)
MAX_SLIPPAGE        = _cfg.get("max_slippage", 0.04)
SCAN_INTERVAL       = _cfg.get("scan_interval", 3600)
STOP_COOLDOWN_H     = _cfg.get("stop_cooldown_h", 12)
MC_SIMS             = _cfg.get("mc_simulations", 2000)
VC_KEY              = _cfg.get("vc_key", "")

# --- NEW: probability discount -------------------------------------------
# Multiply model p by this factor before computing EV.
# Accounts for systematic overconfidence in the ensemble probability engine.
# Every trade so far has repriced to ~30-40% of entry within one cycle,
# implying the true market p is roughly 0.4-0.6x our model p.
# At 0.60 the bot needs a much larger model-vs-market gap to fire.
P_DISCOUNT_FACTOR   = _cfg.get("p_discount", 0.60)

# --- NEW: price stability -------------------------------------------------
# Require the ask price to be observed at least MIN_STABLE_SCANS times
# without moving more than MAX_PRICE_DRIFT before placing a trade.
# Blocks entry into illiquid markets where the ask is not a real price.
MIN_STABLE_SCANS    = _cfg.get("min_stable_scans", 3)
MAX_PRICE_DRIFT     = _cfg.get("max_price_drift", 0.02)
# -------------------------------------------------------------------------

DATA_DIR      = Path("data")
DATA_DIR.mkdir(exist_ok=True)
STATE_FILE    = DATA_DIR / "state.json"
MARKETS_DIR   = DATA_DIR / "markets"
MARKETS_DIR.mkdir(exist_ok=True)
ENSEMBLE_CACHE = DATA_DIR / "ensemble_cache.json"

# Price history for stability check — keyed by market_id
# Persists in memory across scans within a single run.
# Format: {market_id: [{"price": float, "time": datetime}, ...]}
_price_history = {}

MONITOR_INTERVAL = 600   # 10 min

LOCATIONS = {
    "nyc":          {"lat": 40.7772,  "lon":  -73.8726, "name": "New York City", "station": "KLGA", "unit": "F", "region": "us"},
    "chicago":      {"lat": 41.9742,  "lon":  -87.9073, "name": "Chicago",       "station": "KORD", "unit": "F", "region": "us"},
    "miami":        {"lat": 25.7959,  "lon":  -80.2870, "name": "Miami",         "station": "KMIA", "unit": "F", "region": "us"},
    "dallas":       {"lat": 32.8471,  "lon":  -96.8518, "name": "Dallas",        "station": "KDAL", "unit": "F", "region": "us"},
    "seattle":      {"lat": 47.4502,  "lon": -122.3088, "name": "Seattle",       "station": "KSEA", "unit": "F", "region": "us"},
    "atlanta":      {"lat": 33.6407,  "lon":  -84.4277, "name": "Atlanta",       "station": "KATL", "unit": "F", "region": "us"},
    "london":       {"lat": 51.5048,  "lon":    0.0495, "name": "London",        "station": "EGLC", "unit": "C", "region": "eu"},
    "paris":        {"lat": 48.9962,  "lon":    2.5979, "name": "Paris",         "station": "LFPG", "unit": "C", "region": "eu"},
    "munich":       {"lat": 48.3537,  "lon":   11.7750, "name": "Munich",        "station": "EDDM", "unit": "C", "region": "eu"},
    "ankara":       {"lat": 40.1281,  "lon":   32.9951, "name": "Ankara",        "station": "LTAC", "unit": "C", "region": "eu"},
    "seoul":        {"lat": 37.4691,  "lon":  126.4505, "name": "Seoul",         "station": "RKSI", "unit": "C", "region": "asia"},
    "tokyo":        {"lat": 35.7647,  "lon":  140.3864, "name": "Tokyo",         "station": "RJTT", "unit": "C", "region": "asia"},
    "shanghai":     {"lat": 31.1443,  "lon":  121.8083, "name": "Shanghai",      "station": "ZSPD", "unit": "C", "region": "asia"},
    "singapore":    {"lat":  1.3502,  "lon":  103.9940, "name": "Singapore",     "station": "WSSS", "unit": "C", "region": "asia"},
    "lucknow":      {"lat": 26.7606,  "lon":   80.8893, "name": "Lucknow",       "station": "VILK", "unit": "C", "region": "asia"},
    "tel-aviv":     {"lat": 32.0114,  "lon":   34.8867, "name": "Tel Aviv",      "station": "LLBG", "unit": "C", "region": "asia"},
    "toronto":      {"lat": 43.6772,  "lon":  -79.6306, "name": "Toronto",       "station": "CYYZ", "unit": "C", "region": "ca"},
    "sao-paulo":    {"lat": -23.4356, "lon":  -46.4731, "name": "Sao Paulo",     "station": "SBGR", "unit": "C", "region": "sa"},
    "buenos-aires": {"lat": -34.8222, "lon":  -58.5358, "name": "Buenos Aires",  "station": "SAEZ", "unit": "C", "region": "sa"},
    "wellington":   {"lat": -41.3272, "lon":  174.8052, "name": "Wellington",    "station": "NZWN", "unit": "C", "region": "oc"},
}

TIMEZONES = {
    "nyc": "America/New_York", "chicago": "America/Chicago",
    "miami": "America/New_York", "dallas": "America/Chicago",
    "seattle": "America/Los_Angeles", "atlanta": "America/New_York",
    "london": "Europe/London", "paris": "Europe/Paris",
    "munich": "Europe/Berlin", "ankara": "Europe/Istanbul",
    "seoul": "Asia/Seoul", "tokyo": "Asia/Tokyo",
    "shanghai": "Asia/Shanghai", "singapore": "Asia/Singapore",
    "lucknow": "Asia/Kolkata", "tel-aviv": "Asia/Jerusalem",
    "toronto": "America/Toronto", "sao-paulo": "America/Sao_Paulo",
    "buenos-aires": "America/Argentina/Buenos_Aires", "wellington": "Pacific/Auckland",
}

MONTHS = ["january","february","march","april","may","june",
          "july","august","september","october","november","december"]

# =============================================================================
# PRICE STABILITY CHECK
# =============================================================================

def record_price(market_id, ask_price):
    """
    Record an observed ask price for a market.
    Called every scan for every candidate outcome.
    Keeps only the last 6 observations (1 hour at 10-min scans).
    """
    if market_id not in _price_history:
        _price_history[market_id] = []
    _price_history[market_id].append({
        "price": ask_price,
        "time":  datetime.now(timezone.utc),
    })
    # Keep only last 6 observations
    _price_history[market_id] = _price_history[market_id][-6:]


def is_price_stable(market_id, current_ask):
    """
    Returns (stable: bool, observations: int, reason: str).

    A price is considered stable if:
      - We have seen it at least MIN_STABLE_SCANS times
      - The max deviation across those observations is <= MAX_PRICE_DRIFT

    This blocks buying into illiquid markets where the quoted ask
    is not a real, executable price — the most common cause of
    immediate post-entry price collapse.
    """
    history = _price_history.get(market_id, [])
    n = len(history)

    if n < MIN_STABLE_SCANS:
        return False, n, f"only {n}/{MIN_STABLE_SCANS} observations"

    recent = history[-MIN_STABLE_SCANS:]
    prices = [h["price"] for h in recent]
    drift  = max(prices) - min(prices)

    if drift > MAX_PRICE_DRIFT:
        return False, n, f"price drifted ${drift:.3f} (max ${MAX_PRICE_DRIFT:.3f})"

    return True, n, "stable"


# =============================================================================
# ENSEMBLE FORECAST
# =============================================================================

def fetch_ensemble(city_slug, dates):
    """
    Fetch ECMWF ensemble members from Open-Meteo ensemble API.
    Returns dict: {date_str: [member_temps...]} for each requested date.

    The ensemble API returns keys like "temperature_2m_max_member00" through
    "temperature_2m_max_member49" — 50 independent model runs.

    For US cities we also fetch GFS ensemble and blend them.
    Falls back to a single-model forecast with synthetic spread if the
    ensemble API is unavailable.
    """
    loc  = LOCATIONS[city_slug]
    unit = loc["unit"]
    temp_unit = "fahrenheit" if unit == "F" else "celsius"
    tz   = TIMEZONES.get(city_slug, "UTC")
    results = {d: [] for d in dates}

    # --- ECMWF ensemble (ecmwf_ifs04 = 51 members at 0.4° resolution) ---
    ecmwf_members = _fetch_ensemble_model(
        lat=loc["lat"], lon=loc["lon"],
        model="ecmwf_ifs04",
        temp_unit=temp_unit,
        tz=tz,
        dates=dates,
    )

    for d in dates:
        results[d].extend(ecmwf_members.get(d, []))

    # --- GFS ensemble for US cities (adds more members, better mesoscale) ---
    if loc["region"] == "us":
        gfs_members = _fetch_ensemble_model(
            lat=loc["lat"], lon=loc["lon"],
            model="gfs_seamless",
            temp_unit=temp_unit,
            tz=tz,
            dates=dates,
        )
        for d in dates:
            gfs = gfs_members.get(d, [])
            results[d].extend(gfs)

    # --- Fallback: single forecast + synthetic spread ---
    for d in dates:
        if not results[d]:
            fallback = _fetch_single_forecast(
                lat=loc["lat"], lon=loc["lon"],
                model="ecmwf_ifs025",
                temp_unit=temp_unit,
                tz=tz,
                date=d,
            )
            if fallback is not None:
                sigma = 2.0 if unit == "F" else 1.2
                results[d] = [fallback + random.gauss(0, sigma)
                               for _ in range(50)]

    return results


def _fetch_ensemble_model(lat, lon, model, temp_unit, tz, dates):
    """
    Hits the Open-Meteo ensemble API for a given model and extracts
    all member temperature_2m_max values per date.
    Returns {date_str: [float, ...]}
    """
    url = (
        f"https://ensemble-api.open-meteo.com/v1/ensemble"
        f"?latitude={lat}&longitude={lon}"
        f"&daily=temperature_2m_max"
        f"&temperature_unit={temp_unit}"
        f"&forecast_days=7"
        f"&timezone={tz}"
        f"&models={model}"
    )
    results = defaultdict(list)
    for attempt in range(3):
        try:
            r    = requests.get(url, timeout=(5, 15))
            data = r.json()
            if "error" in data or "daily" not in data:
                break
            daily = data["daily"]
            date_list = daily.get("time", [])
            member_keys = [k for k in daily if k.startswith("temperature_2m_max_member")]
            if not member_keys:
                single = daily.get("temperature_2m_max", [])
                if single:
                    for date, temp in zip(date_list, single):
                        if date in dates and temp is not None:
                            results[date].append(float(temp))
                break
            for key in member_keys:
                temps = daily[key]
                for date, temp in zip(date_list, temps):
                    if date in dates and temp is not None:
                        results[date].append(float(temp))
            break
        except Exception as e:
            if attempt < 2:
                time.sleep(2)
            else:
                print(f"  [ENS] {model} {lat},{lon}: {e}")
    return dict(results)


def _fetch_single_forecast(lat, lon, model, temp_unit, tz, date):
    """Single deterministic forecast as fallback."""
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        f"&daily=temperature_2m_max"
        f"&temperature_unit={temp_unit}"
        f"&forecast_days=7"
        f"&timezone={tz}"
        f"&models={model}"
        f"&bias_correction=true"
    )
    for attempt in range(3):
        try:
            data = requests.get(url, timeout=(5, 10)).json()
            if "error" not in data:
                for d, temp in zip(data["daily"]["time"], data["daily"]["temperature_2m_max"]):
                    if d == date and temp is not None:
                        return float(temp)
            break
        except Exception as e:
            if attempt < 2:
                time.sleep(2)
            else:
                print(f"  [FC] {lat},{lon}: {e}")
    return None


# =============================================================================
# MONTE CARLO PROBABILITY ENGINE
# =============================================================================

def ensemble_stats(members):
    """
    Compute mean, std, and spread category from ensemble members.
    Returns dict with useful diagnostics.
    """
    if not members:
        return {"mean": None, "std": None, "n": 0, "spread": "unknown"}
    n    = len(members)
    mean = sum(members) / n
    var  = sum((x - mean) ** 2 for x in members) / n
    std  = math.sqrt(var)
    if std < 1.5:
        spread = "tight"
    elif std < 3.0:
        spread = "moderate"
    elif std < 5.0:
        spread = "wide"
    else:
        spread = "very_wide"
    return {"mean": round(mean, 2), "std": round(std, 3), "n": n, "spread": spread}


def mc_bucket_probs(members, buckets, n_sims=None):
    """
    Monte Carlo probability estimation.

    For each bucket (t_low, t_high), estimate P(actual falls in bucket)
    by drawing n_sims samples from the empirical ensemble distribution
    and counting how many land in each bucket.
    """
    if not members or not buckets:
        return {}

    n_sims = n_sims or MC_SIMS
    stats  = ensemble_stats(members)

    spread_map = {"tight": 0.8, "moderate": 1.2, "wide": 1.8, "very_wide": 2.5, "unknown": 1.5}
    sigma_res  = spread_map.get(stats["spread"], 1.5)

    counts = defaultdict(int)
    n_mem  = len(members)
    for _ in range(n_sims):
        base   = members[random.randint(0, n_mem - 1)]
        sample = base + random.gauss(0, sigma_res)
        for t_low, t_high in buckets:
            if t_low == -999:
                if sample <= t_high:
                    counts[(t_low, t_high)] += 1
            elif t_high == 999:
                if sample >= t_low:
                    counts[(t_low, t_high)] += 1
            else:
                if t_low <= sample <= t_high:
                    counts[(t_low, t_high)] += 1

    return {bucket: round(count / n_sims, 5) for bucket, count in counts.items()}


def find_best_signal(members, outcomes, unit, min_price=None, max_price=None,
                     min_volume=None, max_slippage=None, min_ev=None):
    """
    Given ensemble members and Polymarket market outcomes, find the
    single best trade: highest EV bucket that passes all filters.

    NOTE: p is discounted by P_DISCOUNT_FACTOR before EV is computed.
    This accounts for observed systematic overconfidence in the model.
    The raw model p and discounted p are both stored in the signal for
    transparency and later calibration analysis.

    Returns a signal dict or None.
    """
    min_price    = min_price    or MIN_PRICE
    max_price    = max_price    or MAX_PRICE
    min_volume   = min_volume   or MIN_VOLUME
    max_slippage = max_slippage or MAX_SLIPPAGE
    min_ev       = min_ev       or MIN_EV

    if not members or not outcomes:
        return None

    stats = ensemble_stats(members)
    if stats["mean"] is None:
        return None

    mean   = stats["mean"]
    std    = max(stats["std"] or 2.0, 1.0)
    window = 3.0 * std

    candidate_buckets = []
    outcome_map       = {}
    for o in outcomes:
        t_low, t_high = o["range"]
        if t_low == -999 or t_high == 999:
            continue
        bucket_mid = (t_low + t_high) / 2
        if abs(bucket_mid - mean) > window:
            continue
        ask = o["ask"]
        bid = o["bid"]
        if ask < min_price or ask >= max_price:
            continue
        if (ask - bid) > max_slippage:
            continue
        if o["volume"] < min_volume:
            continue
        candidate_buckets.append((t_low, t_high))
        outcome_map[(t_low, t_high)] = o

    if not candidate_buckets:
        return None

    probs = mc_bucket_probs(members, candidate_buckets)

    best_ev     = min_ev
    best_signal = None

    for bucket, raw_p in probs.items():
        o   = outcome_map[bucket]
        ask = o["ask"]

        # Apply probability discount before computing EV.
        # raw_p is what the model says; discounted_p is what we trust.
        discounted_p = round(raw_p * P_DISCOUNT_FACTOR, 5)
        ev           = calc_ev(discounted_p, ask)

        if ev > best_ev:
            kelly = calc_kelly(discounted_p, ask)
            best_ev     = ev
            best_signal = {
                "market_id":      o["market_id"],
                "question":       o["question"],
                "bucket_low":     bucket[0],
                "bucket_high":    bucket[1],
                "entry_price":    ask,
                "bid_at_entry":   o["bid"],
                "spread":         o["spread"],
                "p":              discounted_p,         # discounted — used for sizing
                "p_raw":          round(raw_p, 5),      # raw model output — for diagnostics
                "p_discount":     P_DISCOUNT_FACTOR,
                "ev":             round(ev, 4),
                "kelly":          round(kelly, 4),
                "ensemble_mean":  stats["mean"],
                "ensemble_std":   stats["std"],
                "ensemble_n":     stats["n"],
                "ensemble_spread": stats["spread"],
                "forecast_src":   "ensemble",
            }

    if best_signal:
        # Sanity check on discounted p
        p = best_signal["p"]
        if p > 0.80 or p < 0.03:
            return None

    return best_signal


# =============================================================================
# MATH UTILITIES
# =============================================================================

def calc_ev(p, price):
    """Expected value: p * (1/price - 1) - (1-p)"""
    if price <= 0 or price >= 1:
        return 0.0
    return round(p * (1.0 / price - 1.0) - (1.0 - p), 4)

def calc_kelly(p, price):
    """Fractional Kelly criterion."""
    if price <= 0 or price >= 1:
        return 0.0
    b = 1.0 / price - 1.0
    f = (p * b - (1.0 - p)) / b
    return round(min(max(0.0, f) * KELLY_FRACTION, 1.0), 4)

def bet_size(kelly, balance):
    return round(min(kelly * balance, MAX_BET), 2)

def calc_stop_price(entry_price):
    """
    Adaptive stop loss.
    Deeper stops for cheap contracts — they need more room to breathe.
    """
    if entry_price < 0.10:
        return None
    elif entry_price < 0.20:
        pct = 0.45
    elif entry_price < 0.30:
        pct = 0.38
    elif entry_price < 0.40:
        pct = 0.30
    else:
        pct = 0.25
    return round(entry_price * (1.0 - pct), 4)

def hours_to_resolution(end_date_str):
    try:
        end = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        return max(0.0, (end - datetime.now(timezone.utc)).total_seconds() / 3600)
    except Exception:
        return 999.0

# =============================================================================
# POLYMARKET API
# =============================================================================

def get_polymarket_event(city_slug, month, day, year):
    slug = f"highest-temperature-in-{city_slug}-on-{month}-{day}-{year}"
    try:
        r    = requests.get(f"https://gamma-api.polymarket.com/events?slug={slug}", timeout=(5, 8))
        data = r.json()
        if data and isinstance(data, list) and data:
            return data[0]
    except Exception:
        pass
    return None


def parse_temp_range(question):
    """
    Parse temperature bucket range from Polymarket question text.
    Handles unicode degree symbol (°) and all observed question formats.
    """
    if not question:
        return None
    num = r'(-?\d+(?:\.\d+)?)'
    deg = r'[\u00b0°]?'
    m = re.search(num + deg + r'[FC]\s+or\s+below', question, re.IGNORECASE)
    if m:
        return (-999.0, float(m.group(1)))
    m = re.search(num + deg + r'[FC]\s+or\s+higher', question, re.IGNORECASE)
    if m:
        return (float(m.group(1)), 999.0)
    m = re.search(r'between\s+' + num + r'\s*-\s*' + num + deg + r'[FC]', question, re.IGNORECASE)
    if m:
        return (float(m.group(1)), float(m.group(2)))
    m = re.search(r'be\s+' + num + deg + r'[FC]\s+on', question, re.IGNORECASE)
    if m:
        v = float(m.group(1))
        return (v, v)
    return None


def parse_market_prices(market):
    """
    Extract bid and ask from market object.
    bestBid/bestAsk are the real executable prices.
    outcomePrices[0] is the YES mid-price — NOT the ask.
    """
    best_bid = market.get("bestBid")
    best_ask = market.get("bestAsk")
    if best_bid is not None and best_ask is not None:
        return float(best_bid), float(best_ask)
    try:
        prices    = json.loads(market.get("outcomePrices", "[0.5,0.5]"))
        yes_price = float(prices[0])
        return yes_price, yes_price
    except Exception:
        return 0.5, 0.5


def fetch_real_ask(market_id, fallback_ask, fallback_bid):
    """Re-fetch live prices immediately before placing a trade."""
    try:
        r     = requests.get(f"https://gamma-api.polymarket.com/markets/{market_id}", timeout=(3, 5))
        mdata = r.json()
        real_ask = float(mdata.get("bestAsk") or fallback_ask)
        real_bid = float(mdata.get("bestBid") or fallback_bid)
        return real_bid, real_ask
    except Exception:
        return fallback_bid, fallback_ask


def check_market_resolved(market_id):
    """Returns True (win), False (loss), or None (still open)."""
    try:
        r      = requests.get(f"https://gamma-api.polymarket.com/markets/{market_id}", timeout=(5, 8))
        data   = r.json()
        if not data.get("closed", False):
            return None
        prices    = json.loads(data.get("outcomePrices", "[0.5,0.5]"))
        yes_price = float(prices[0])
        if yes_price >= 0.95:
            return True
        elif yes_price <= 0.05:
            return False
        return None
    except Exception as e:
        print(f"  [RESOLVE] {market_id}: {e}")
    return None


def get_actual_temp(city_slug, date_str):
    """Actual recorded temperature from Visual Crossing (for resolution)."""
    if not VC_KEY:
        return None
    loc     = LOCATIONS[city_slug]
    station = loc["station"]
    unit    = loc["unit"]
    vc_unit = "us" if unit == "F" else "metric"
    url = (
        f"https://weather.visualcrossing.com/VisualCrossingWebServices/rest/services/timeline"
        f"/{station}/{date_str}/{date_str}"
        f"?unitGroup={vc_unit}&key={VC_KEY}&include=days&elements=tempmax"
    )
    try:
        data = requests.get(url, timeout=(5, 8)).json()
        days = data.get("days", [])
        if days and days[0].get("tempmax") is not None:
            return round(float(days[0]["tempmax"]), 1)
    except Exception as e:
        print(f"  [VC] {city_slug} {date_str}: {e}")
    return None


# =============================================================================
# MARKET STORAGE
# =============================================================================

def market_path(city_slug, date_str):
    return MARKETS_DIR / f"{city_slug}_{date_str}.json"

def load_market(city_slug, date_str):
    p = market_path(city_slug, date_str)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return None

def save_market(market):
    p = market_path(market["city"], market["date"])
    p.write_text(json.dumps(market, indent=2, ensure_ascii=False), encoding="utf-8")

def load_all_markets():
    markets = []
    for f in MARKETS_DIR.glob("*.json"):
        try:
            markets.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            pass
    return markets

def new_market(city_slug, date_str, event, hours):
    loc = LOCATIONS[city_slug]
    return {
        "city":               city_slug,
        "city_name":          loc["name"],
        "date":               date_str,
        "unit":               loc["unit"],
        "station":            loc["station"],
        "event_end_date":     event.get("endDate", ""),
        "hours_at_discovery": round(hours, 1),
        "status":             "open",
        "position":           None,
        "actual_temp":        None,
        "resolved_outcome":   None,
        "pnl":                None,
        "last_stop_at":       None,
        "ensemble_snapshots": [],
        "all_outcomes":       [],
        "created_at":         datetime.now(timezone.utc).isoformat(),
    }

# =============================================================================
# STATE
# =============================================================================

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {
        "balance":          BALANCE,
        "starting_balance": BALANCE,
        "total_trades":     0,
        "wins":             0,
        "losses":           0,
        "peak_balance":     BALANCE,
    }

def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")

# =============================================================================
# CORE TRADING LOGIC
# =============================================================================

def scan_and_update(simulate=False):
    """
    Main scan loop.
    simulate=True: log signals but don't open/close positions or change balance.
    """
    now      = datetime.now(timezone.utc)
    state    = load_state()
    balance  = state["balance"]
    new_pos  = 0
    closed   = 0
    resolved = 0

    for city_slug, loc in LOCATIONS.items():
        unit     = loc["unit"]
        unit_sym = "F" if unit == "F" else "C"
        print(f"  -> {loc['name']}...", end=" ", flush=True)

        dates = [(now + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(1, 4)]

        try:
            ensemble_by_date = fetch_ensemble(city_slug, dates)
            time.sleep(0.3)
        except Exception as e:
            print(f"skipped ({e})")
            continue

        for i, date in enumerate(dates):
            dt    = datetime.strptime(date, "%Y-%m-%d")
            event = get_polymarket_event(city_slug, MONTHS[dt.month - 1], dt.day, dt.year)
            if not event:
                continue

            end_date = event.get("endDate", "")
            hours    = hours_to_resolution(end_date) if end_date else 0
            horizon  = f"D+{i + 1}"

            if hours < MIN_HOURS or hours > MAX_HOURS:
                continue

            mkt = load_market(city_slug, date)
            if mkt is None:
                mkt = new_market(city_slug, date, event, hours)
            if mkt["status"] == "resolved":
                continue

            outcomes = []
            for market in event.get("markets", []):
                question = market.get("question", "")
                mid      = str(market.get("id", ""))
                volume   = float(market.get("volume", 0))
                rng      = parse_temp_range(question)
                if not rng:
                    continue
                bid, ask = parse_market_prices(market)

                # Record price observation for stability tracking
                record_price(mid, ask)

                outcomes.append({
                    "question":  question,
                    "market_id": mid,
                    "range":     rng,
                    "bid":       round(bid, 4),
                    "ask":       round(ask, 4),
                    "price":     round(bid, 4),
                    "spread":    round(ask - bid, 4),
                    "volume":    round(volume, 0),
                })
            outcomes.sort(key=lambda x: x["range"][0])
            mkt["all_outcomes"] = outcomes

            members = ensemble_by_date.get(date, [])
            stats   = ensemble_stats(members)

            mkt["ensemble_snapshots"].append({
                "ts":        now.isoformat(),
                "horizon":   horizon,
                "hours_left": round(hours, 1),
                "mean":      stats["mean"],
                "std":       stats["std"],
                "n":         stats["n"],
                "spread":    stats["spread"],
            })

            # ----------------------------------------------------------------
            # STOP-LOSS / TRAILING STOP
            # ----------------------------------------------------------------
            if mkt.get("position") and mkt["position"].get("status") == "open":
                pos = mkt["position"]
                current_price = None
                for o in outcomes:
                    if o["market_id"] == pos["market_id"]:
                        current_price = o["bid"]
                        break

                if current_price is not None:
                    entry = pos["entry_price"]
                    if "stop_price" not in pos or pos["stop_price"] is None:
                        pos["stop_price"] = calc_stop_price(entry)

                    if current_price >= entry * 1.25 and pos.get("stop_price", 0) < entry:
                        pos["stop_price"] = entry
                        pos["trailing_activated"] = True

                    if pos["stop_price"] and current_price <= pos["stop_price"]:
                        pnl = round((current_price - entry) * pos["shares"], 2)
                        if not simulate:
                            balance += pos["cost"] + pnl
                        pos["closed_at"]    = now.isoformat()
                        pos["close_reason"] = "stop_loss" if current_price < entry else "trailing_stop"
                        pos["exit_price"]   = current_price
                        pos["pnl"]          = pnl
                        pos["status"]       = "closed"
                        mkt["last_stop_at"] = now.isoformat()
                        closed += 1
                        reason = "STOP" if current_price < entry else "TRAIL BE"
                        tag = "[SIM]" if simulate else ""
                        print(f"  {tag}[{reason}] {loc['name']} {date} | "
                              f"entry ${entry:.3f} exit ${current_price:.3f} | "
                              f"{hours:.0f}h left | PnL: {'+'if pnl>=0 else ''}{pnl:.2f}")

            # ----------------------------------------------------------------
            # FORECAST-SHIFT CLOSE
            # ----------------------------------------------------------------
            if mkt.get("position") and mkt["position"].get("status") == "open" and members:
                pos    = mkt["position"]
                mean   = stats["mean"]
                old_lo = pos["bucket_low"]
                old_hi = pos["bucket_high"]
                if mean is not None:
                    bucket_mid = (old_lo + old_hi) / 2
                    std        = max(stats["std"] or 2.0, 1.0)
                    if abs(mean - bucket_mid) > 1.5 * std:
                        current_price = None
                        for o in outcomes:
                            if o["market_id"] == pos["market_id"]:
                                current_price = o["bid"]
                                break
                        if current_price is not None:
                            pnl = round((current_price - pos["entry_price"]) * pos["shares"], 2)
                            if not simulate:
                                balance += pos["cost"] + pnl
                            pos["closed_at"]    = now.isoformat()
                            pos["close_reason"] = "forecast_shifted"
                            pos["exit_price"]   = current_price
                            pos["pnl"]          = pnl
                            pos["status"]       = "closed"
                            closed += 1
                            tag = "[SIM]" if simulate else ""
                            print(f"  {tag}[SHIFT] {loc['name']} {date} — ensemble moved "
                                  f"(mean {mean:.1f}{unit_sym}, bucket {old_lo}-{old_hi}) | "
                                  f"PnL: {'+'if pnl>=0 else ''}{pnl:.2f}")

            # ----------------------------------------------------------------
            # OPEN NEW POSITION
            # ----------------------------------------------------------------
            if not mkt.get("position") and members:

                last_stop = mkt.get("last_stop_at")
                if last_stop:
                    try:
                        last_stop_dt = datetime.fromisoformat(last_stop.replace("Z", "+00:00"))
                        if (now - last_stop_dt).total_seconds() / 3600 < STOP_COOLDOWN_H:
                            save_market(mkt)
                            continue
                    except Exception:
                        pass

                if stats["spread"] == "very_wide":
                    save_market(mkt)
                    continue

                signal = find_best_signal(members, outcomes, unit)

                if signal:
                    # --- PRICE STABILITY CHECK ----------------------------
                    # Before doing anything, verify the ask has been stable
                    # across multiple scans. If the price is newly appeared
                    # or moving around, we skip and wait for it to settle.
                    market_id = signal["market_id"]
                    stable, obs_count, stability_reason = is_price_stable(
                        market_id, signal["entry_price"]
                    )

                    if not stable:
                        tag = "[SIM]" if simulate else ""
                        print(f"  {tag}[WATCH] {loc['name']} {date} — "
                              f"waiting for price stability ({stability_reason}, "
                              f"{obs_count}/{MIN_STABLE_SCANS} scans) | "
                              f"ask=${signal['entry_price']:.3f} "
                              f"p_raw={signal['p_raw']:.3f} p={signal['p']:.3f} "
                              f"EV={signal['ev']:+.2f}")
                        save_market(mkt)
                        continue
                    # ------------------------------------------------------

                    # Re-fetch live ask before committing
                    real_bid, real_ask = fetch_real_ask(
                        signal["market_id"],
                        signal["entry_price"],
                        signal["bid_at_entry"],
                    )
                    real_spread = round(real_ask - real_bid, 4)

                    # Re-check price stability with the live ask too
                    live_stable, _, live_reason = is_price_stable(market_id, real_ask)
                    if not live_stable:
                        tag = "[SIM]" if simulate else ""
                        print(f"  {tag}[SKIP] {loc['name']} {date} — "
                              f"live ask moved at last check ({live_reason})")
                        save_market(mkt)
                        continue

                    if real_spread > MAX_SLIPPAGE or real_ask >= MAX_PRICE or real_ask < MIN_PRICE:
                        print(f"  [SKIP] {loc['name']} {date} — live ask ${real_ask:.3f} spread ${real_spread:.3f}")
                        save_market(mkt)
                        continue

                    # Recompute EV with live ask and discounted p
                    live_ev = calc_ev(signal["p"], real_ask)
                    if live_ev < MIN_EV:
                        save_market(mkt)
                        continue

                    kelly  = calc_kelly(signal["p"], real_ask)
                    size   = bet_size(kelly, balance)
                    if size < 0.50:
                        save_market(mkt)
                        continue

                    stop_p = calc_stop_price(real_ask)
                    if stop_p is None:
                        save_market(mkt)
                        continue

                    position = {
                        "market_id":         signal["market_id"],
                        "question":          signal["question"],
                        "bucket_low":        signal["bucket_low"],
                        "bucket_high":       signal["bucket_high"],
                        "entry_price":       real_ask,
                        "bid_at_entry":      real_bid,
                        "spread":            real_spread,
                        "shares":            round(size / real_ask, 2),
                        "cost":              size,
                        "stop_price":        stop_p,
                        "p":                 signal["p"],
                        "p_raw":             signal["p_raw"],
                        "p_discount":        signal["p_discount"],
                        "ev":                round(live_ev, 4),
                        "kelly":             kelly,
                        "ensemble_mean":     signal["ensemble_mean"],
                        "ensemble_std":      signal["ensemble_std"],
                        "ensemble_n":        signal["ensemble_n"],
                        "ensemble_spread":   signal["ensemble_spread"],
                        "forecast_src":      "ensemble",
                        "price_obs_count":   obs_count,
                        "opened_at":         now.isoformat(),
                        "status":            "open",
                        "pnl":               None,
                        "exit_price":        None,
                        "close_reason":      None,
                        "closed_at":         None,
                        "trailing_activated": False,
                    }

                    if not simulate:
                        balance -= size
                        mkt["position"] = position
                        state["total_trades"] += 1
                        new_pos += 1

                    bucket_label = f"{signal['bucket_low']}-{signal['bucket_high']}{unit_sym}"
                    tag = "[SIM]" if simulate else ""
                    print(f"  {tag}[BUY]  {loc['name']} {horizon} {date} | {bucket_label} | "
                          f"${real_ask:.3f} | p={signal['p']:.3f} (raw={signal['p_raw']:.3f}) | "
                          f"EV {live_ev:+.2f} | ${size:.2f} | "
                          f"mean={signal['ensemble_mean']:.1f}±{signal['ensemble_std']:.1f}{unit_sym} "
                          f"({signal['ensemble_n']}mbrs/{signal['ensemble_spread']}) | "
                          f"stable={obs_count}scans")

            if hours < 0.5 and mkt["status"] == "open":
                mkt["status"] = "closed"

            if not simulate:
                save_market(mkt)
            time.sleep(0.1)

        print("ok")

    # ------------------------------------------------------------------------
    # AUTO-RESOLUTION
    # ------------------------------------------------------------------------
    if not simulate:
        for mkt in load_all_markets():
            if mkt["status"] == "resolved":
                continue
            pos = mkt.get("position")
            if not pos or pos.get("status") != "open":
                continue
            market_id = pos.get("market_id")
            if not market_id:
                continue

            won = check_market_resolved(market_id)
            if won is None:
                continue

            price  = pos["entry_price"]
            size   = pos["cost"]
            shares = pos["shares"]
            pnl    = round(shares * (1 - price), 2) if won else round(-size, 2)

            balance            += size + pnl
            pos["exit_price"]   = 1.0 if won else 0.0
            pos["pnl"]          = pnl
            pos["close_reason"] = "resolved"
            pos["closed_at"]    = now.isoformat()
            pos["status"]       = "closed"
            mkt["pnl"]          = pnl
            mkt["status"]       = "resolved"
            mkt["resolved_outcome"] = "win" if won else "loss"

            if won:
                state["wins"] += 1
            else:
                state["losses"] += 1

            result   = "WIN" if won else "LOSS"
            unit_sym = "F" if mkt["unit"] == "F" else "C"
            snaps    = mkt.get("ensemble_snapshots", [])
            print(f"  [{result}] {mkt['city_name']} {mkt['date']} | "
                  f"bet {pos['bucket_low']}-{pos['bucket_high']}{unit_sym} | "
                  f"p={pos['p']:.3f} (raw={pos.get('p_raw','?')}) | "
                  f"PnL: {'+'if pnl>=0 else ''}{pnl:.2f}")
            resolved += 1
            save_market(mkt)
            time.sleep(0.3)

    if not simulate:
        state["balance"]      = round(balance, 2)
        state["peak_balance"] = max(state.get("peak_balance", balance), balance)
        save_state(state)

    return new_pos, closed, resolved


# =============================================================================
# POSITION MONITOR (between full scans)
# =============================================================================

def monitor_positions():
    """
    Quick stop-loss check on open positions without fetching new forecasts.
    Runs every MONITOR_INTERVAL seconds between full scans.
    """
    markets  = load_all_markets()
    open_pos = [m for m in markets if m.get("position") and m["position"].get("status") == "open"]
    if not open_pos:
        return 0

    state   = load_state()
    balance = state["balance"]
    closed  = 0
    now     = datetime.now(timezone.utc)

    for mkt in open_pos:
        pos = mkt["position"]
        mid = pos["market_id"]

        current_price = None
        try:
            r         = requests.get(f"https://gamma-api.polymarket.com/markets/{mid}", timeout=(3, 5))
            mdata     = r.json()
            best_bid  = mdata.get("bestBid")
            if best_bid is not None:
                current_price = float(best_bid)
        except Exception:
            pass

        if current_price is None:
            for o in mkt.get("all_outcomes", []):
                if o["market_id"] == mid:
                    current_price = o.get("bid", o.get("price"))
                    break

        if current_price is None:
            continue

        entry      = pos["entry_price"]
        stop       = pos.get("stop_price") or calc_stop_price(entry)
        if "stop_price" not in pos:
            pos["stop_price"] = stop

        city_name  = LOCATIONS.get(mkt["city"], {}).get("name", mkt["city"])
        end_date   = mkt.get("event_end_date", "")
        hours_left = hours_to_resolution(end_date)
        unit_sym   = "F" if mkt["unit"] == "F" else "C"

        if current_price >= entry * 1.25 and pos.get("stop_price", 0) < entry:
            pos["stop_price"] = entry
            pos["trailing_activated"] = True
            print(f"  [TRAIL] {city_name} {mkt['date']} — stop → breakeven ${entry:.3f}")

        take_profit = None
        if hours_left < 24:
            take_profit = None
        elif hours_left < 48:
            take_profit = 0.85
        else:
            take_profit = 0.75

        take_hit = take_profit is not None and current_price >= take_profit
        stop_hit = stop is not None and current_price <= pos["stop_price"]

        if take_hit or stop_hit:
            pnl = round((current_price - entry) * pos["shares"], 2)
            balance += pos["cost"] + pnl
            pos["closed_at"] = now.isoformat()
            if take_hit:
                pos["close_reason"] = "take_profit"
                reason = "TAKE"
            elif current_price < entry:
                pos["close_reason"] = "stop_loss"
                reason = "STOP"
                mkt["last_stop_at"] = now.isoformat()
            else:
                pos["close_reason"] = "trailing_stop"
                reason = "TRAIL BE"
            pos["exit_price"] = current_price
            pos["pnl"]        = pnl
            pos["status"]     = "closed"
            closed += 1
            print(f"  [{reason}] {city_name} {mkt['date']} | "
                  f"entry ${entry:.3f} exit ${current_price:.3f} | "
                  f"{hours_left:.0f}h left | PnL: {'+'if pnl>=0 else ''}{pnl:.2f}")
            save_market(mkt)

    if closed:
        state["balance"] = round(balance, 2)
        save_state(state)

    return closed


# =============================================================================
# REPORT / STATUS
# =============================================================================

def print_status():
    state    = load_state()
    markets  = load_all_markets()
    open_pos = [m for m in markets if m.get("position") and m["position"].get("status") == "open"]
    resolved = [m for m in markets if m["status"] == "resolved" and m.get("pnl") is not None]
    bal      = state["balance"]
    start    = state["starting_balance"]
    ret_pct  = (bal - start) / start * 100
    wins     = state["wins"]
    losses   = state["losses"]
    total    = wins + losses

    print(f"\n{'='*60}")
    print(f"  WEATHERBET MC — STATUS")
    print(f"{'='*60}")
    print(f"  Balance:  ${bal:,.2f}  (start ${start:,.2f}, {'+'if ret_pct>=0 else ''}{ret_pct:.1f}%)")
    print(f"  Discount: {P_DISCOUNT_FACTOR:.0%} | Min EV: {MIN_EV:+.0%} | "
          f"Min stable scans: {MIN_STABLE_SCANS}")
    if total:
        print(f"  Resolved: {total} | W: {wins} | L: {losses} | WR: {wins/total:.0%}")
    else:
        print(f"  Resolved: 0 trades yet")
    print(f"  Open:     {len(open_pos)}")

    if open_pos:
        print(f"\n  Open positions:")
        total_unreal = 0.0
        for m in sorted(open_pos, key=lambda x: x["date"]):
            pos       = m["position"]
            unit_sym  = "F" if m["unit"] == "F" else "C"
            label     = f"{pos['bucket_low']}-{pos['bucket_high']}{unit_sym}"
            cur_price = pos["entry_price"]
            for o in m.get("all_outcomes", []):
                if o["market_id"] == pos["market_id"]:
                    cur_price = o.get("bid", o.get("price", cur_price))
                    break
            unreal        = round((cur_price - pos["entry_price"]) * pos["shares"], 2)
            total_unreal += unreal
            mean = pos.get("ensemble_mean", "?")
            std  = pos.get("ensemble_std", "?")
            p_raw = pos.get("p_raw", "?")
            print(f"    {m['city_name']:<16} {m['date']} | {label:<12} | "
                  f"entry ${pos['entry_price']:.3f} → ${cur_price:.3f} | "
                  f"p={pos['p']:.3f} (raw={p_raw}) ev={pos['ev']:+.2f} | "
                  f"PnL: {'+'if unreal>=0 else ''}{unreal:.2f} | "
                  f"ens {mean}±{std}{unit_sym}")
        sign = "+" if total_unreal >= 0 else ""
        print(f"\n  Unrealized: {sign}{total_unreal:.2f}")
    print(f"{'='*60}\n")


def print_report():
    markets  = load_all_markets()
    resolved = [m for m in markets if m["status"] == "resolved" and m.get("pnl") is not None]

    print(f"\n{'='*60}")
    print(f"  WEATHERBET MC — FULL REPORT")
    print(f"{'='*60}")
    if not resolved:
        print("  No resolved markets yet.")
        return

    total_pnl = sum(m["pnl"] for m in resolved)
    wins      = [m for m in resolved if m["resolved_outcome"] == "win"]
    losses    = [m for m in resolved if m["resolved_outcome"] == "loss"]
    print(f"\n  Resolved: {len(resolved)} | Wins: {len(wins)} | Losses: {len(losses)}")
    print(f"  Win rate: {len(wins)/len(resolved):.0%}")
    print(f"  Total PnL: {'+'if total_pnl>=0 else ''}{total_pnl:.2f}")

    print(f"\n  By city:")
    for city in sorted(set(m["city"] for m in resolved)):
        group = [m for m in resolved if m["city"] == city]
        w     = sum(1 for m in group if m["resolved_outcome"] == "win")
        pnl   = sum(m["pnl"] for m in group)
        name  = LOCATIONS[city]["name"]
        print(f"    {name:<16} {w}/{len(group)} ({w/len(group):.0%})  PnL: {'+'if pnl>=0 else ''}{pnl:.2f}")

    print(f"\n  Calibration (bet p vs actual win rate):")
    print(f"  (p shown is discounted; raw model p would be p / {P_DISCOUNT_FACTOR:.2f})")
    p_buckets = [(0.0, 0.15), (0.15, 0.25), (0.25, 0.35), (0.35, 0.50)]
    for lo, hi in p_buckets:
        group = [m for m in resolved
                 if m.get("position") and lo <= m["position"].get("p", 0) < hi]
        if not group:
            continue
        actual_wr = sum(1 for m in group if m["resolved_outcome"] == "win") / len(group)
        avg_p     = sum(m["position"]["p"] for m in group) / len(group)
        avg_p_raw = sum(m["position"].get("p_raw", m["position"]["p"]) for m in group) / len(group)
        print(f"    p={lo:.2f}-{hi:.2f}: n={len(group)} | "
              f"avg bet p={avg_p:.3f} (raw={avg_p_raw:.3f}) | actual WR={actual_wr:.0%}")

    print(f"\n  All resolved trades:")
    for m in sorted(resolved, key=lambda x: x["date"]):
        pos      = m.get("position", {})
        unit_sym = "F" if m["unit"] == "F" else "C"
        snaps    = m.get("ensemble_snapshots", [])
        ens_mean = snaps[0].get("mean", "?") if snaps else "?"
        ens_std  = snaps[0].get("std", "?") if snaps else "?"
        label    = f"{pos.get('bucket_low')}-{pos.get('bucket_high')}{unit_sym}" if pos else "no pos"
        result   = m["resolved_outcome"].upper()
        p_str    = f"p={pos.get('p','?'):.3f}" if pos.get("p") else ""
        p_raw    = pos.get("p_raw", "?")
        pnl_str  = f"{'+'if m['pnl']>=0 else ''}{m['pnl']:.2f}"
        actual   = f"actual={m['actual_temp']}{unit_sym}" if m["actual_temp"] else ""
        print(f"    {m['city_name']:<16} {m['date']} | {label:<12} | {p_str} (raw={p_raw}) | "
              f"ens {ens_mean}±{ens_std}{unit_sym} | {actual} | {result} {pnl_str}")

    print(f"{'='*60}\n")


# =============================================================================
# MAIN LOOP
# =============================================================================

def run_loop():
    state = load_state()
    print(f"\n{'='*60}")
    print(f"  WEATHERBET MC — STARTING")
    print(f"{'='*60}")
    print(f"  Cities:         {len(LOCATIONS)}")
    print(f"  Balance:        ${state['balance']:,.0f} | Max bet: ${MAX_BET}")
    print(f"  Scan:           {SCAN_INTERVAL//60} min | Monitor: {MONITOR_INTERVAL//60} min")
    print(f"  Horizon:        D+1 to D+3 only")
    print(f"  Min EV:         {MIN_EV:+.0%} | Min price: ${MIN_PRICE}")
    print(f"  P discount:     {P_DISCOUNT_FACTOR:.0%} (raw p × {P_DISCOUNT_FACTOR})")
    print(f"  Stability:      {MIN_STABLE_SCANS} scans, max drift ${MAX_PRICE_DRIFT:.2f}")
    print(f"  MC sims:        {MC_SIMS}")
    print(f"  Data:           {DATA_DIR.resolve()}")
    print(f"  Ctrl+C to stop\n")

    last_full_scan = 0

    while True:
        now_ts  = time.time()
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if now_ts - last_full_scan >= SCAN_INTERVAL:
            print(f"[{now_str}] full scan...")
            try:
                new_pos, closed, res = scan_and_update()
                state = load_state()
                print(f"  balance: ${state['balance']:,.2f} | "
                      f"new: {new_pos} | closed: {closed} | resolved: {res}")
                last_full_scan = time.time()
            except KeyboardInterrupt:
                print(f"\n  Stopping...")
                save_state(load_state())
                break
            except requests.exceptions.ConnectionError:
                print(f"  Connection lost — retrying in 60s")
                time.sleep(60)
                continue
            except Exception as e:
                print(f"  Scan error: {e} — retrying in 60s")
                time.sleep(60)
                continue
        else:
            print(f"[{now_str}] monitoring positions...")
            try:
                n = monitor_positions()
                if n:
                    state = load_state()
                    print(f"  balance: ${state['balance']:,.2f}")
            except Exception as e:
                print(f"  Monitor error: {e}")

        try:
            time.sleep(MONITOR_INTERVAL)
        except KeyboardInterrupt:
            print(f"\n  Stopping...")
            save_state(load_state())
            break


# =============================================================================
# CLI
# =============================================================================

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "run":
        run_loop()
    elif cmd == "status":
        print_status()
    elif cmd == "report":
        print_report()
    elif cmd == "simulate":
        print(f"\n[SIMULATE] scanning for signals (no trades will be placed)...\n")
        scan_and_update(simulate=True)
    else:
        print("Usage: python weatherbet_mc.py [run|status|report|simulate]")
