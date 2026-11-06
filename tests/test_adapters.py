import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import duckdb
import h5py
import numpy as np
import pandas as pd
import pytest

import adapters.common as common
import adapters.ved as ved
import adapters.zenodo_can as zen


def rows(path, table="signals"):
    return duckdb.sql(f"SELECT * FROM read_parquet('{path.as_posix()}/{table}/*/*.parquet') ORDER BY seq").df()


# ---------------------------------------------------------------- VED
@pytest.fixture
def ved_out(tmp_path, monkeypatch):
    mapping = common.load_mapping(ved.MAPPING)
    raw_cols = [m["raw"] for m in mapping["signals"] if "raw" in m]
    raw_cols += ["Air Conditioning Power[kW]", "Air Conditioning Power[Watts]"]
    df = pd.DataFrame({c: [np.nan, np.nan] for c in raw_cols})
    df.insert(0, "DayNum", [1.5, 1.5])  # README: DayNum 1.5 = 1 Nov 2017 12:00
    df.insert(1, "VehId", [7, 7])
    df.insert(2, "Trip", [1, 1])
    df.insert(3, "Timestamp(ms)", [0, 1000])
    df["Vehicle Speed[km/h]"] = [10.0, 12.0]
    df["Air Conditioning Power[kW]"] = [1.2, np.nan]
    df["Air Conditioning Power[Watts]"] = [np.nan, 300.0]
    typed = tmp_path / "ved.parquet"
    df.to_parquet(typed, index=False)

    monkeypatch.setattr(common, "PROCESSED", tmp_path / "processed")
    monkeypatch.setattr(ved, "TYPED", typed)
    monkeypatch.setattr(ved, "OUT", tmp_path / "processed" / "ved")
    monkeypatch.setattr(ved, "load_static", lambda: pd.DataFrame({"VehId": [7], "EngineType": ["EV"]}))
    ved.convert([7])
    return tmp_path / "processed" / "ved" / "ved_7"


def test_ved_daynum_is_local_time(ved_out):
    sig = rows(ved_out)
    first = sig.ts.min().to_pydatetime().astimezone(timezone.utc)
    # 12:00 in Ann Arbor on 1 Nov 2017 (EDT, UTC-4) is 16:00 UTC
    assert first == datetime(2017, 11, 1, 16, 0, tzinfo=timezone.utc)


def test_ved_ac_power_columns_are_merged_and_nulls_dropped(ved_out):
    sig = rows(ved_out)
    ac = sig[sig.signal_path == "Vehicle.VendorExt.Cabin.AirConditioningPower"].value.tolist()
    assert ac == [1200.0, 300.0]
    assert set(sig.signal_path) == {"Vehicle.Speed", "Vehicle.VendorExt.Cabin.AirConditioningPower"}
    assert set(sig.source_type) == {"recorded"} and set(sig.source_id) == {"ved_7"}


def test_ved_trip_boundaries_become_ignition_events(ved_out):
    ev = rows(ved_out, "events")
    assert ev.event_type.tolist() == ["IGNITION_ON", "IGNITION_OFF"]
    assert json.loads(ev["attrs"].iloc[0])["trip"] == 1


# ---------------------------------------------------------------- Zenodo
def make_hdf(path, mapping, unit_override=None):
    t = np.arange(0, 2.0, 0.05)  # 2 s at 20 Hz
    with h5py.File(path, "w") as f:
        f.attrs["Start time"] = np.array([b"14.11.2018 08:31:55"])
        f.attrs["Event2"] = np.array([b"Notice - Time code lost at 14.11.2018 08:31:56"])
        channels = {m["raw"]: m["raw_unit"] for m in mapping["signals"]}
        channels.update({raw: "-" for raw in mapping["lamps"]})
        for group in mapping["cruise"].values():
            channels.update({raw: "-" for raw in group})
        for raw, unit in channels.items():
            value = np.full_like(t, 2893.0 if "Latitude" in raw else 1.0)
            if raw == "CAN/ENG_Hotlamp":
                value = np.where((t > 0.5) & (t < 1.5), 1.0, 0.0)
            if unit_override and raw == unit_override[0]:
                unit = unit_override[1]
            ds = f.create_dataset(raw, data=np.column_stack([value, t]))  # column 0 = value, 1 = time
            ds.attrs["Unit"] = np.array([unit.encode("latin-1")])


def test_zenodo_value_time_order_scaling_and_events(tmp_path):
    mapping = common.load_mapping(zen.MAPPING)
    p = tmp_path / "20181114_Driver2_Trip1.hdf"
    make_hdf(p, mapping)
    sig, ev, info = zen.convert_file(p, mapping, ZoneInfo(mapping["timezone"]))

    speed = sig[sig.signal_path == "Vehicle.Speed"]
    assert speed.ts.min() == pd.Timestamp("2018-11-14 07:31:55", tz="UTC")  # CET = UTC+1
    assert len(speed) == 20  # decimated 20 Hz -> 10 Hz over 2 s
    lat = sig[sig.signal_path == "Vehicle.CurrentLocation.Latitude"].value.iloc[0]
    assert lat == pytest.approx(2893.0 / 60)

    lamp = ev[ev.event_type.str.startswith("WARNING_LAMP")]
    assert lamp.event_type.tolist() == ["WARNING_LAMP_ON", "WARNING_LAMP_OFF"]
    assert json.loads(lamp["attrs"].iloc[0]) == {"lamp": "engine_overtemperature"}
    assert info["logger_notices"] == 1
    assert (ev.event_type == "LOGGER_TIMECODE_LOST").sum() == 1


def test_zenodo_unit_mismatch_is_rejected(tmp_path):
    mapping = common.load_mapping(zen.MAPPING)
    p = tmp_path / "20181114_Driver2_Trip9.hdf"
    make_hdf(p, mapping, unit_override=("CAN/VehicleSpeed", "m/s"))
    with pytest.raises(ValueError, match="unit"):
        zen.convert_file(p, mapping, ZoneInfo(mapping["timezone"]))


def test_mapping_rejects_unknown_signal(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("signals:\n  - {raw: x, path: Vehicle.Not.A.Signal}\n")
    with pytest.raises(ValueError, match="not in schema"):
        common.load_mapping(bad)
