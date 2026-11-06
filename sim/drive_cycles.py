"""Synthetic 1 Hz speed profiles for city and highway trips.

Returns per-second speed (m/s) and a per-second road-type label. The label goes to
ground truth only; the vehicle data never says whether a second was "city" or "highway".
"""

import numpy as np

KMH = 1 / 3.6


def _ramp(v0: float, v1: float, accel: float) -> list[float]:
    n = max(1, int(abs(v1 - v0) / accel))
    return list(np.linspace(v0, v1, n + 1)[1:])


def city_segment(rng, distance_m: float, vmax_kmh: float = 55.0) -> list[float]:
    out: list[float] = []
    dist = 0.0
    while dist < distance_m:
        target = rng.uniform(25, vmax_kmh) * KMH
        seg = _ramp(0.0, target, rng.uniform(1.0, 1.6))
        seg += list(target + rng.normal(0, 0.4, int(rng.uniform(15, 90))).clip(-target, None))
        seg += _ramp(target, 0.0, rng.uniform(1.2, 2.0))
        seg += [0.0] * int(rng.uniform(5, 40))
        out += seg
        dist += float(np.sum(seg))
    return out


def highway_segment(rng, distance_m: float, cruise_kmh: float) -> list[float]:
    out = _ramp(0.0, cruise_kmh * KMH, rng.uniform(1.2, 1.8))
    v = cruise_kmh * KMH
    dist = float(np.sum(out))
    slow_left = 0
    slow_target = v
    while dist < distance_m:
        if slow_left == 0 and rng.random() < 1 / 600:
            slow_left = int(rng.uniform(30, 90))
            slow_target = rng.uniform(60, 80) * KMH
        goal = slow_target if slow_left > 0 else cruise_kmh * KMH
        slow_left = max(0, slow_left - 1)
        # first-order approach to the goal speed plus Ornstein-Uhlenbeck-like noise
        v += 0.08 * (goal - v) + rng.normal(0, 0.15)
        v = max(0.0, v)
        out.append(v)
        dist += v
    out += _ramp(v, 0.0, rng.uniform(1.0, 1.5))
    return out


def trip_profile(rng, kind: str, distance_km: float, towing: bool = False) -> tuple[np.ndarray, np.ndarray]:
    """kind: 'city' | 'highway'. Highway trips include city access legs at both ends."""
    if kind == "city":
        v = city_segment(rng, distance_km * 1000)
        labels = ["city"] * len(v)
    else:
        access = min(3000.0, distance_km * 1000 * 0.1)
        cruise = rng.uniform(85, 90) if towing else rng.uniform(100, 125)
        a = city_segment(rng, access)
        h = highway_segment(rng, distance_km * 1000 - 2 * access, cruise)
        b = city_segment(rng, access)
        v = a + h + b
        labels = ["city"] * len(a) + ["highway"] * len(h) + ["city"] * len(b)
    return np.asarray(v, dtype=float), np.asarray(labels)
