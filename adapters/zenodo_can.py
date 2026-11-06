"""Zenodo CAN adapter: data/raw/zenodo_can/*.hdf -> data/processed/zenodo_can/<source_id>/{signals,events}.

    python -m adapters.zenodo_can

Signals are decimated to the rate in zenodo_mapping.yaml. Warning lamps and cruise-control buttons
become events; the logger's "Time code lost/restored" notices are kept as data-quality events.
Unreadable files are skipped and listed in ingest_report.json.
"""

import json
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import h5py
import numpy as np
import pandas as pd

from adapters.common import (EVENT_COLUMNS, PROCESSED, ROOT, SIGNAL_COLUMNS, connect, copy_partitioned,
                             load_mapping, write_report)
from memory.store import write_source_meta

RAW = ROOT / "data" / "raw" / "zenodo_can"
OUT = PROCESSED / "zenodo_can"
MAPPING = Path(__file__).with_name("zenodo_mapping.yaml")
NOTICE = re.compile(r"Time code (lost|restored) at (\d\d\.\d\d\.\d{4} \d\d:\d\d:\d\d)")
LOGGER_TIME = "%d.%m.%Y %H:%M:%S"


def attr_text(value) -> str:
    if isinstance(value, np.ndarray):
        value = value.flat[0]
    return value.decode("latin-1") if isinstance(value, bytes) else str(value)


def read_channel(f: h5py.File, name: str) -> tuple[np.ndarray, np.ndarray, str]:
    """Return (time_s, value, unit). Column 0 is the value, column 1 the time."""
    ds = f[name]
    a = ds[:]
    return a[:, 1].astype(float), a[:, 0].astype(float), attr_text(ds.attrs.get("Unit", ""))


def decimate(t: np.ndarray, v: np.ndarray, rate_hz: float) -> tuple[np.ndarray, np.ndarray]:
    """Keep the first sample of every 1/rate_hz bucket."""
    _, idx = np.unique(np.floor(t * rate_hz).astype(np.int64), return_index=True)
    return t[idx], v[idx]


def edges(t: np.ndarray, v: np.ndarray, debounce_s: float) -> tuple[list[float], list[float]]:
    on = v > 0.5
    rising = t[1:][on[1:] & ~on[:-1]]
    falling = t[1:][~on[1:] & on[:-1]]

    def debounce(ts):
        kept: list[float] = []
        for x in ts:
            if not kept or x - kept[-1] >= debounce_s:
                kept.append(float(x))
        return kept

    return debounce(rising), debounce(falling)


def convert_file(path: Path, mapping: dict, tz: ZoneInfo) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    f = h5py.File(path, "r")
    start_local = datetime.strptime(attr_text(f.attrs["Start time"]), LOGGER_TIME).replace(tzinfo=tz)
    t0 = start_local.timestamp()
    trip = path.stem  # e.g. 20181114_Driver2_Trip1
    driver = trip.split("_")[1]

    frames = []
    for m in mapping["signals"]:
        t, v, unit = read_channel(f, m["raw"])
        if unit != m["raw_unit"]:
            raise ValueError(f"{path.name}:{m['raw']} has unit {unit!r}, mapping expects {m['raw_unit']!r}")
        t, v = decimate(t, v, float(m.get("rate_hz", mapping["default_rate_hz"])))
        frames.append(pd.DataFrame({"t": t, "signal_path": m["path"], "unit": m["unit"],
                                    "value": v * float(m.get("scale", 1.0))}))
    sig = pd.concat(frames, ignore_index=True)
    sig["ts"] = pd.to_datetime((t0 + sig.pop("t")) * 1000, unit="ms", utc=True)

    t_end = float(f["CAN/VehicleSpeed"][-1, 1])
    ev = [(t0, "IGNITION_ON", "body", "info", "", {"trip": trip, "driver": driver, "derived": "recording start"}),
          (t0 + t_end, "IGNITION_OFF", "body", "info", "", {"trip": trip, "driver": driver, "derived": "recording end"})]
    debounce = float(mapping["edge_debounce_s"])
    for raw, lamp in mapping["lamps"].items():
        t, v, _ = read_channel(f, raw)
        up, down = edges(t, v, debounce)
        ev += [(t0 + x, "WARNING_LAMP_ON", "instrument_cluster", "warning", "", {"lamp": lamp}) for x in up]
        ev += [(t0 + x, "WARNING_LAMP_OFF", "instrument_cluster", "info", "", {"lamp": lamp}) for x in down]
    for kind, buttons in (("ADAS_ENGAGED", mapping["cruise"]["engage"]), ("ADAS_DISENGAGED", mapping["cruise"]["disengage"])):
        for raw, button in buttons.items():
            t, v, _ = read_channel(f, raw)
            up, _ = edges(t, v, debounce)
            key = "trigger" if kind == "ADAS_ENGAGED" else "reason"
            ev += [(t0 + x, kind, "adas", "info", "", {"system": "cruise_control", key: button}) for x in up]
    notices = 0
    for name in f.attrs:
        m = NOTICE.search(attr_text(f.attrs[name]))
        if m:
            ts = datetime.strptime(m.group(2), LOGGER_TIME).replace(tzinfo=tz).timestamp()
            ev.append((ts, f"LOGGER_TIMECODE_{m.group(1).upper()}", "logger", "info", "", {"note": attr_text(f.attrs[name])}))
            notices += 1
    events = pd.DataFrame(ev, columns=["t", "event_type", "subsystem", "severity", "code", "attrs"])
    events["ts"] = pd.to_datetime(events.pop("t") * 1000, unit="ms", utc=True)
    events["attrs"] = [json.dumps(a, sort_keys=True) for a in events["attrs"]]
    events["trip"] = trip

    info = {"start_local": start_local.isoformat(), "duration_min": round(t_end / 60, 1),
            "signal_rows": len(sig), "events": len(events), "logger_notices": notices,
            "lamp_events": int((events.event_type == "WARNING_LAMP_ON").sum()),
            "cruise_events": int(events.event_type.str.startswith("ADAS").sum())}
    return sig, events, info


def convert() -> dict:
    mapping = load_mapping(MAPPING)
    tz = ZoneInfo(mapping["timezone"])
    source_id = mapping["source_id"]
    report = {"dataset": "zenodo_can", "source_id": source_id, "timezone_assumed": mapping["timezone"],
              "files": {}, "skipped": {}}
    sigs, evs = [], []
    for path in sorted(RAW.glob("*.hdf")):
        try:
            sig, events, info = convert_file(path, mapping, tz)
        except OSError as e:  # truncated / unreadable HDF5
            report["skipped"][path.name] = str(e).split(" (")[0][:160]
            print(f"  skipped {path.name}: unreadable HDF5")
            continue
        sigs.append(sig)
        evs.append(events)
        report["files"][path.name] = info
        print(f"  {path.stem:<24} {info['duration_min']:>6} min  rows={info['signal_rows']:>9,}  "
              f"events={info['events']} (logger notices {info['logger_notices']}, cruise {info['cruise_events']}, lamps {info['lamp_events']})")

    con = connect()
    con.register("sig", pd.concat(sigs, ignore_index=True))
    con.register("ev", pd.concat(evs, ignore_index=True))
    common = f"'{source_id}' AS source_id, '{mapping['scenario_id']}' AS scenario_id, '{mapping['source_type']}' AS source_type"
    copy_partitioned(con, f"""SELECT {SIGNAL_COLUMNS}, date FROM (
        SELECT ts, signal_path, value, unit, 'valid' AS quality, {common},
               row_number() OVER (ORDER BY ts, signal_path) AS seq, CAST(ts AS DATE) AS date FROM sig)""",
                     OUT / source_id / "signals")
    copy_partitioned(con, f"""SELECT {EVENT_COLUMNS}, date FROM (
        SELECT ts, '{source_id}-ev' || lpad(CAST(row_number() OVER (ORDER BY ts, event_type) AS VARCHAR), 6, '0') AS event_id,
               event_type, subsystem, severity, code, attrs, {common},
               row_number() OVER (ORDER BY ts, event_type) AS seq, CAST(ts AS DATE) AS date FROM ev)""",
                     OUT / source_id / "events")
    write_source_meta(OUT / source_id, source_id=source_id, dataset="zenodo_can", source_type="recorded",
                      timezone=mapping["timezone"], description="Zenodo record 2661316, CAN bus data with GPS")
    write_report(OUT / "ingest_report.json", report)
    return report


def main():
    r = convert()
    rows = sum(i["signal_rows"] for i in r["files"].values())
    print(f"zenodo_can: {len(r['files'])} files converted, {len(r['skipped'])} skipped, {rows:,} signal rows")


if __name__ == "__main__":
    main()
