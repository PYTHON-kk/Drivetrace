"""Validate the generated synthetic dataset and write docs/dataset_report.md.

    python -m tools.validate_dataset

Two kinds of checks:
  * integrity  - ranges, monotonic odometer, coverage, file sizes;
  * answerability - the case-study queries computed with DuckDB from the vehicle data alone
    (labels are only used afterwards to confirm the scenarios behaved as designed).
"""

import json
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
SIM = ROOT / "data" / "sim"
REPORT = ROOT / "docs" / "dataset_report.md"
TZ = "Europe/Berlin"
P = "Vehicle.Powertrain.TractionBattery."
V = "Vehicle.VendorExt.TractionBattery.Cooling."


def dir_size_mb(path: Path) -> float:
    return sum(f.stat().st_size for f in path.rglob("*.parquet")) / 1e6


def table(rows, headers) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    for r in rows:
        out.append("| " + " | ".join("" if v is None else str(v) for v in r) + " |")
    return "\n".join(out)


def connect(run_dir: Path) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute(f"SET TimeZone='{TZ}'")
    con.execute(f"CREATE VIEW sig AS SELECT * FROM read_parquet('{run_dir.as_posix()}/signals/*/*.parquet')")
    con.execute(f"CREATE VIEW ev AS SELECT * FROM read_parquet('{run_dir.as_posix()}/events/*/*.parquet')")
    con.execute("""
        CREATE TABLE trips AS
        WITH ign AS (
            SELECT ts, event_type, lead(ts) OVER (ORDER BY ts) AS next_ts, lead(event_type) OVER (ORDER BY ts) AS next_type
            FROM ev WHERE event_type IN ('IGNITION_ON', 'IGNITION_OFF'))
        SELECT row_number() OVER (ORDER BY ts) AS trip, ts AS t_on, next_ts AS t_off
        FROM ign WHERE event_type = 'IGNITION_ON' AND next_type = 'IGNITION_OFF'
    """)
    con.execute(f"""
        CREATE TABLE trip_stats AS
        SELECT t.trip, t.t_on, t.t_off, CAST(timezone('{TZ}', t.t_on) AS DATE) AS local_day,
               max(s.value) FILTER (WHERE s.signal_path = 'Vehicle.TraveledDistance')
                 - min(s.value) FILTER (WHERE s.signal_path = 'Vehicle.TraveledDistance') AS km,
               -sum(s.value) FILTER (WHERE s.signal_path = '{P}CurrentPower') / 3600.0 AS wh,
               avg(s.value) FILTER (WHERE s.signal_path = 'Vehicle.Speed') AS avg_kmh,
               avg(s.value) FILTER (WHERE s.signal_path = 'Vehicle.Exterior.AirTemperature') AS ambient_c,
               avg(s.value) FILTER (WHERE s.signal_path = 'Vehicle.VendorExt.Cabin.HeaterPower') AS heater_w
        FROM trips t JOIN sig s ON s.ts >= t.t_on AND s.ts < t.t_off
        GROUP BY ALL
    """)
    return con


def validate_history(lines: list[str]):
    labels_path = next((SIM / "labels").glob("history_*.json"))
    labels = json.loads(labels_path.read_text())
    run_dir = SIM / "history" / labels["run_id"]
    con = connect(run_dir)

    lines.append(f"## 1. Six-week history `{labels['run_id']}`\n")
    span = con.execute("SELECT min(ts), max(ts), count(*), count(DISTINCT signal_path) FROM sig").fetchone()
    n_days = con.execute(f"SELECT count(DISTINCT CAST(timezone('{TZ}', ts) AS DATE)) FROM sig").fetchone()[0]
    ts = con.execute("SELECT count(*), round(sum(km),1), round(sum(wh)/1000,1) FROM trip_stats").fetchone()
    lines.append(table([
        ["Time span (local)", f"{span[0]:%Y-%m-%d %H:%M} to {span[1]:%Y-%m-%d %H:%M}"],
        ["Local days with data", n_days],
        ["Signal rows / distinct signals", f"{span[2]:,} / {span[3]}"],
        ["Events", f"{labels['counts']['events']:,}"],
        ["Trips / km / traction+aux energy", f"{ts[0]} / {ts[1]} km / {ts[2]} kWh"],
        ["Parquet size on disk", f"{dir_size_mb(run_dir):.1f} MB"],
    ], ["Item", "Value"]))

    # ---- integrity ---------------------------------------------------------
    checks = []
    bad_soc = con.execute(f"SELECT count(*) FROM sig WHERE signal_path='{P}StateOfCharge.Current' AND (value<0 OR value>100)").fetchone()[0]
    checks.append(["SoC within 0-100 %", bad_soc == 0, f"{bad_soc} violations"])
    back = con.execute("""SELECT count(*) FROM (SELECT value - lag(value) OVER (ORDER BY ts, seq) d FROM sig
                          WHERE signal_path='Vehicle.TraveledDistance') WHERE d < -1e-6""").fetchone()[0]
    checks.append(["Odometer never decreases", back == 0, f"{back} decreases"])
    nan = con.execute("SELECT count(*) FROM sig WHERE isnan(value) OR value IS NULL").fetchone()[0]
    checks.append(["No NaN/NULL values", nan == 0, f"{nan} rows"])
    leak = con.execute("SELECT count(DISTINCT scenario_id) FROM sig").fetchone()[0]
    checks.append(["scenario_id is the run id only (no fault label leak)", leak == 1, f"{leak} distinct"])
    checks.append(["At least 42 local days (NFR-06)", n_days >= 42, f"{n_days} days"])
    lines.append("\n**Integrity checks**\n")
    lines.append(table([[c[0], "PASS" if c[1] else "FAIL", c[2]] for c in checks], ["Check", "Result", "Detail"]))

    # ---- answerability: case-study queries from vehicle data only --------
    lines.append("\n**Case-study queries answered from the data (no labels used)**\n")

    q1 = con.execute(f"""SELECT strftime(timezone('{TZ}', ts), '%H:%M') AS t, json_extract_string(attrs, '$.reason'),
                         json_extract_string(attrs, '$.speed_kmh') FROM ev
                         WHERE event_type='ADAS_DISENGAGED' AND CAST(timezone('{TZ}', ts) AS DATE) = DATE '2026-10-27'
                         ORDER BY ts""").fetchall()
    lines.append(f"*Q1 - ADAS disengagements on 2026-10-27 (\"yesterday\" for a 28 Oct vehicle clock):* {len(q1)} events\n")
    lines.append(table(q1[:12], ["Local time", "Reason", "Speed km/h"]))

    daily = con.execute("""SELECT local_day, round(sum(wh)/sum(km),1) AS wh_km, round(sum(km),1) km,
                           round(avg(ambient_c),1) amb FROM trip_stats WHERE km > 1 GROUP BY 1 ORDER BY 1""").fetchall()
    worst = con.execute("""WITH d AS (SELECT local_day, sum(wh)/sum(km) wh_km FROM trip_stats WHERE km > 5 GROUP BY 1)
                           SELECT local_day, round(wh_km,1), round(wh_km - lag(wh_km) OVER (ORDER BY local_day),1) AS jump
                           FROM d ORDER BY jump DESC NULLS LAST LIMIT 3""").fetchall()
    lines.append("\n*Q2 - largest day-over-day drop in energy efficiency (rise in Wh/km):*\n")
    lines.append(table(worst, ["Local day", "Wh/km", "Increase vs previous driving day"]))

    two = con.execute("""SELECT CASE WHEN local_day > DATE '2026-10-14' THEN 'last 14 days' ELSE 'previous 14 days' END AS period,
                                round(sum(wh)/sum(km),1), round(avg(ambient_c),1), round(avg(heater_w)),
                                round(avg(avg_kmh),1)
                         FROM trip_stats WHERE local_day > DATE '2026-09-30' AND km > 1 GROUP BY 1 ORDER BY 1""").fetchall()
    rng = con.execute(f"""SELECT CASE WHEN CAST(timezone('{TZ}', ts) AS DATE) > DATE '2026-10-14' THEN 'last 14 days' ELSE 'previous 14 days' END,
                                 round(avg(value)/1000,1) FROM sig WHERE signal_path='Vehicle.Powertrain.Range'
                          AND CAST(timezone('{TZ}', ts) AS DATE) > DATE '2026-09-30' GROUP BY 1 ORDER BY 1""").fetchall()
    rng_map = dict(rng)
    lines.append("\n*Q3 - why has range reduced over the last two weeks? (inputs for the explanation):*\n")
    lines.append(table([list(r) + [rng_map.get(r[0])] for r in two],
                       ["Period", "Wh/km", "Ambient degC", "Cabin heater W", "Avg speed km/h", "Displayed range km (avg)"]))

    cvh = con.execute(f"""WITH s AS (SELECT CASE WHEN value > 70 THEN 'highway-like (>70 km/h)' ELSE 'city-like (<=70 km/h)' END AS cls, ts
                                      FROM sig WHERE signal_path='Vehicle.Speed' AND value > 0.5),
                          p AS (SELECT ts, value FROM sig WHERE signal_path='{P}CurrentPower'),
                          v AS (SELECT ts, value FROM sig WHERE signal_path='Vehicle.Speed')
                          SELECT s.cls, round(-sum(p.value)/3600/1000, 1) kwh, round(sum(v.value)/3600, 1) km,
                                 round(-sum(p.value)/3600 / (sum(v.value)/3600), 1) wh_km
                          FROM s JOIN p USING (ts) JOIN v USING (ts) GROUP BY 1 ORDER BY 1""").fetchall()
    lines.append("\n*Q4 - city vs highway energy consumption (classified from speed only):*\n")
    lines.append(table(cvh, ["Class", "kWh", "km", "Wh/km"]))

    warns = con.execute(f"""SELECT ts, strftime(timezone('{TZ}', ts), '%Y-%m-%d %H:%M') t, json_extract_string(attrs,'$.measured_avg_c'),
                            json_extract_string(attrs,'$.measured_max_c'), json_extract_string(attrs,'$.mode')
                            FROM ev WHERE event_type='BATT_OVERTEMP_WARNING_SET' ORDER BY ts""").fetchall()
    lines.append(f"\n*Q5 / D1 - battery over-temperature warnings and the 30 minutes before each:* {len(warns)} warnings\n")
    rows = []
    for w in warns:
        pre = con.execute(f"""SELECT
              round(avg(value) FILTER (WHERE signal_path='{P}CurrentCurrent'),0),
              round(max(value) FILTER (WHERE signal_path='{P}Temperature.Average') - min(value) FILTER (WHERE signal_path='{P}Temperature.Average'),1),
              round(avg(value) FILTER (WHERE signal_path='{P}Temperature.Max') - avg(value) FILTER (WHERE signal_path='{P}Temperature.Average'),1),
              round(avg(value) FILTER (WHERE signal_path='{V}PumpCommand'),2),
              round(avg(value) FILTER (WHERE signal_path='{V}PumpSpeed'),0),
              round(avg(value) FILTER (WHERE signal_path='Vehicle.Exterior.AirTemperature'),1)
            FROM sig WHERE ts BETWEEN TIMESTAMPTZ '{w[0].isoformat()}' - INTERVAL 30 MINUTE AND TIMESTAMPTZ '{w[0].isoformat()}'""").fetchone()
        rows.append([w[1], w[4], w[2], w[3], *pre])
    lines.append(table(rows, ["Local time", "Mode", "Avg degC", "Max degC", "Mean current A (+=charge)",
                              "Avg temp rise", "Max-Avg gap", "Pump cmd duty", "Pump rpm", "Ambient"]))
    lines.append("\nA negative *Max-Avg gap* is physically impossible and points to a sensor fault (S3/S7).\n")

    # ---- confirm injected scenarios (labels used here only) -----------------
    lines.append("\n**Injected scenarios vs observed behaviour (labels used only for this table)**\n")
    rows = []
    for tag, w in labels["scenario_windows"].items():
        n = con.execute(f"""SELECT count(*) FROM ev WHERE event_type='BATT_OVERTEMP_WARNING_SET'
                            AND ts BETWEEN TIMESTAMPTZ '{w['start_utc']}' AND TIMESTAMPTZ '{w['end_utc']}'""").fetchone()[0]
        rows.append([tag, w["scenario"], ", ".join(w.get("expected", [])), w["start_utc"][:16], w["end_utc"][:16], n])
    lines.append(table(rows, ["Window", "Scenario", "Expected hypothesis", "Start UTC", "End UTC", "Warnings in window"]))

    stuck = con.execute(f"""SELECT count(*), count(DISTINCT value) FROM sig WHERE signal_path='{V}PumpSpeed'
                            AND ts BETWEEN TIMESTAMPTZ '2026-10-10 08:30:00+00' AND TIMESTAMPTZ '2026-10-10 11:30:00+00'""").fetchone()
    gap = con.execute(f"""SELECT max(d) FROM (SELECT ts - lag(ts) OVER (ORDER BY ts) d FROM sig WHERE signal_path='{V}PumpSpeed')""").fetchone()[0]
    lines.append(f"\nS5 check: pump-speed samples during the stuck window = {stuck[0]}, distinct values = {stuck[1]} "
                 f"(1 means frozen); longest gap in pump-speed signal = {gap}.\n")
    return daily


def validate_episodes(lines: list[str]):
    splits = json.loads((SIM / "labels" / "splits.json").read_text())
    lines.append("\n## 2. Labelled episodes (S0-S7)\n")
    lines.append(table([[k, len(v)] for k, v in splits.items()], ["Split", "Episodes"]))
    by_sc: dict[str, list] = {}
    total_mb = 0.0
    for f in sorted((SIM / "labels" / "episodes").glob("*.json")):
        lab = json.loads(f.read_text())
        by_sc.setdefault(lab["scenario"], []).append(lab)
        total_mb += dir_size_mb(SIM / "episodes" / lab["run_id"])
    rows = []
    expect_warning = {"S0": False, "S1": True, "S2": True, "S3": True, "S4": False, "S5": False, "S6": True, "S7": True}
    for sc, labs in sorted(by_sc.items()):
        with_warn = sum(1 for l in labs if l["warnings"])
        ok = all(bool(l["warnings"]) == expect_warning[sc] for l in labs)
        amb = [l["base_ambient_c"] for l in labs]
        rows.append([sc, len(labs), ", ".join(labs[0]["scenario_windows"][f"{sc}_trigger"]["expected"]),
                     f"{min(amb):.1f}-{max(amb):.1f}", f"{with_warn}/{len(labs)}",
                     "yes" if expect_warning[sc] else "no", "PASS" if ok else "FAIL"])
    lines.append("\n")
    lines.append(table(rows, ["Scenario", "Episodes", "Expected hypothesis", "Ambient degC", "With warning",
                              "Warning expected", "Result"]))
    lines.append(f"\nEpisode Parquet size on disk: {total_mb:.1f} MB\n")


RANGES = {  # plausibility limits for recorded data (violations are reported, not removed)
    "Vehicle.Speed": (0, 300),
    "Vehicle.Exterior.AirTemperature": (-50, 60),
    "Vehicle.Powertrain.TractionBattery.StateOfCharge.Current": (0, 100),
    "Vehicle.Powertrain.TractionBattery.CurrentVoltage": (0, 1000),
    "Vehicle.CurrentLocation.Latitude": (-90, 90),
    "Vehicle.CurrentLocation.Longitude": (-180, 180),
    "Vehicle.OBD.MAF": (0, 700),
    "Vehicle.Powertrain.CombustionEngine.ECT": (-40, 150),
}
PROCESSED = ROOT / "data" / "processed"


def recorded_sources() -> list[tuple[str, str, Path]]:
    out = []
    for ds in sorted(p for p in PROCESSED.iterdir() if p.is_dir()):
        for src in sorted(p for p in ds.iterdir() if p.is_dir() and (p / "signals").exists()):
            out.append((ds.name, src.name, src))
    return out


def energy_pivot(con, extra_where: str = "") -> str:
    """Per-sample wide view with dt capped at 5 s (VED samples are irregular)."""
    return f"""
        WITH w AS (
            SELECT ts,
                   max(value) FILTER (WHERE signal_path = 'Vehicle.Speed') AS kmh,
                   max(value) FILTER (WHERE signal_path = '{P}CurrentCurrent') AS amp,
                   max(value) FILTER (WHERE signal_path = '{P}CurrentVoltage') AS volt,
                   max(value) FILTER (WHERE signal_path = 'Vehicle.OBD.MAF') AS maf,
                   max(value) FILTER (WHERE signal_path = 'Vehicle.Exterior.AirTemperature') AS oat,
                   max(value) FILTER (WHERE signal_path = 'Vehicle.VendorExt.Cabin.HeaterPower') AS heater
            FROM sig {extra_where} GROUP BY ts)
        SELECT *, least(epoch(lead(ts) OVER (ORDER BY ts) - ts), 5.0) AS dt FROM w"""


def validate_recorded(lines: list[str]):
    from schema.canonical import load_catalogue

    catalogue = load_catalogue()["signals"]
    sources = recorded_sources()
    lines.append("\n## 3. Recorded datasets (adapter output)\n")
    if not sources:
        lines.append("No adapter output found under data/processed/.\n")
        return
    rows, unit_errors, range_rows = [], [], []
    for ds, src, path in sources:
        con = connect(path)
        n, n_sig, t0, t1 = con.execute("SELECT count(*), count(DISTINCT signal_path), min(ts), max(ts) FROM sig").fetchone()
        n_ev = con.execute("SELECT count(*) FROM ev").fetchone()[0]
        dup = con.execute("SELECT count(*) - count(DISTINCT (ts, signal_path)) FROM sig").fetchone()[0]
        rows.append([ds, src, f"{n:,}", n_sig, n_ev, f"{t0:%Y-%m-%d}", f"{t1:%Y-%m-%d}", dup, f"{dir_size_mb(path):.1f}"])
        for sp, unit in con.execute("SELECT DISTINCT signal_path, unit FROM sig").fetchall():
            if sp not in catalogue or catalogue[sp]["unit"] != unit:
                unit_errors.append([src, sp, unit])
        for sp, (lo, hi) in RANGES.items():
            bad = con.execute(f"SELECT count(*), min(value), max(value) FROM sig WHERE signal_path='{sp}' AND (value < {lo} OR value > {hi})").fetchone()
            if bad[0]:
                range_rows.append([src, sp, bad[0], f"{bad[1]:.1f}", f"{bad[2]:.1f}", f"{lo}..{hi}"])
    lines.append(table(rows, ["Dataset", "Source", "Signal rows", "Signals", "Events", "From (UTC)", "To (UTC)",
                              "Duplicate (ts, signal)", "MB"]))
    lines.append(f"\n**Catalogue check:** {'PASS - every signal path and unit is in schema/vss_mapping.yaml' if not unit_errors else 'FAIL'}\n")
    if unit_errors:
        lines.append(table(unit_errors, ["Source", "Signal", "Unit"]))
    lines.append("\n**Plausibility (values kept, flagged for the evidence gate)**\n")
    lines.append(table(range_rows, ["Source", "Signal", "Rows outside", "Min", "Max", "Allowed"]) if range_rows
                 else "No values outside the plausibility limits.\n")

    # ---- real-data answerability -------------------------------------------
    by_name = {src: path for _, src, path in sources}
    for ev_src in ("ved_455", "ved_10"):
        if ev_src not in by_name:
            continue
        con = connect(by_name[ev_src])
        con.execute("SET TimeZone='America/Detroit'")
        monthly = con.execute(f"""
            WITH p AS ({energy_pivot(con)})
            SELECT strftime(ts, '%Y-%m') AS month, round(sum(kmh * dt) / 3600, 0) AS km,
                   round(-sum(amp * volt * dt) / 3600 / nullif(sum(kmh * dt) / 3600, 0), 0) AS wh_km,
                   round(avg(oat), 1) AS oat, round(avg(heater), 0) AS heater_w
            FROM p WHERE dt IS NOT NULL GROUP BY 1 HAVING sum(kmh * dt) / 3600 > 20 ORDER BY 1""").fetchall()
        lines.append(f"\n*Real EV `{ev_src}` (2013 Nissan Leaf, VED): monthly consumption vs outside temperature and heater use*\n")
        lines.append(table(monthly, ["Month (local)", "km", "Wh/km", "Avg outside degC", "Avg heater W"]))

    ice = next((s for s in ("ved_531", "ved_575") if s in by_name), None)
    if ice:
        con = connect(by_name[ice])
        fuel = con.execute(f"""
            WITH p AS ({energy_pivot(con)})
            SELECT CASE WHEN kmh > 70 THEN 'highway-like (>70 km/h)' ELSE 'city-like (<=70 km/h)' END AS cls,
                   round(sum(kmh * dt) / 3600, 0) km,
                   round(100 * sum(maf * dt) / (14.7 * 745.0) / nullif(sum(kmh * dt) / 3600, 0), 1) AS l_100km
            FROM p WHERE dt IS NOT NULL AND kmh > 0 AND maf IS NOT NULL GROUP BY 1 ORDER BY 1""").fetchall()
        lines.append(f"\n*Real ICE `{ice}` (VED): fuel use estimated from MAF (stoichiometric 14.7, 0.745 kg/l petrol), city vs highway*\n")
        lines.append(table(fuel, ["Class", "km", "Estimated l/100 km"]))

    zen = next((p for ds, _, p in sources if ds == "zenodo_can"), None)
    if zen:
        con = connect(zen)
        trips = con.execute("""
            SELECT json_extract_string(e.attrs, '$.trip') trip, strftime(timezone('Europe/Vienna', t.t_on), '%Y-%m-%d %H:%M'),
                   round(max(s.value) FILTER (WHERE s.signal_path='Vehicle.TripMeterReading'), 1),
                   round(max(s.value) FILTER (WHERE s.signal_path='Vehicle.Speed'), 0),
                   round(avg(s.value) FILTER (WHERE s.signal_path='Vehicle.Exterior.AirTemperature'), 1),
                   (SELECT count(*) FROM ev x WHERE x.event_type LIKE 'ADAS%' AND x.ts BETWEEN t.t_on AND t.t_off),
                   (SELECT count(*) FROM ev x WHERE x.event_type LIKE 'LOGGER%' AND x.ts BETWEEN t.t_on AND t.t_off)
            FROM trips t JOIN ev e ON e.ts = t.t_on AND e.event_type = 'IGNITION_ON'
            JOIN sig s ON s.ts >= t.t_on AND s.ts < t.t_off GROUP BY 1, 2, t.t_on, t.t_off ORDER BY 2""").fetchall()
        lines.append("\n*Real CAN trips (Zenodo): trip summary, cruise-control events and logger time-sync notices*\n")
        lines.append(table(trips, ["Trip", "Start (local)", "km", "Max km/h", "Ambient degC", "Cruise events", "Logger notices"]))
        report = json.loads((zen.parent / "ingest_report.json").read_text())
        for name, why in report.get("skipped", {}).items():
            lines.append(f"\nSkipped `{name}`: {why}\n")


def main():
    lines = ["# DriveTrace dataset report", "",
             "Generated by `python -m tools.validate_dataset`. Sections 1-2 are **simulated** data "
             "(`source_type = simulated`) from `sim/scenarios.yaml`. Section 3 is **recorded** public data "
             "converted by the adapters in `adapters/`.", ""]
    validate_history(lines)
    validate_episodes(lines)
    validate_recorded(lines)
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {REPORT}")


if __name__ == "__main__":
    main()
