"""Download the public datasets that can be fetched without an account.

    python -m scripts.fetch_public --list          # show files and sizes, download nothing
    python -m scripts.fetch_public --yes vedzenodo # download VED + Zenodo CAN into data/raw/

Manual downloads (licence acceptance or login required), see data/MANIFEST.md:
  * Mendeley "Real-world EV data: driving and charging" v2 (CC BY-NC 3.0)
  * TUM "Battery and Heating Data in Real Driving Cycles" (IEEE DataPort, free account)
"""

import argparse
import hashlib
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"

VED_BASE = "https://raw.githubusercontent.com/gsoh/VED/master/Data/"
VED_FILES = ["VED_DynamicData_Part1.7z", "VED_DynamicData_Part2.7z",
             "VED_Static_Data_ICE&HEV.xlsx", "VED_Static_Data_PHEV&EV.xlsx"]
ZENODO_RECORD = "2661316"


def zenodo_files() -> list[dict]:
    with urllib.request.urlopen(f"https://zenodo.org/api/records/{ZENODO_RECORD}") as r:
        rec = json.load(r)
    return [{"name": f["key"], "size": f["size"], "url": f["links"]["self"],
             "md5": f["checksum"].split(":", 1)[1]} for f in rec["files"]]


def ved_files() -> list[dict]:
    with urllib.request.urlopen("https://api.github.com/repos/gsoh/VED/contents/Data") as r:
        listing = {f["name"]: f["size"] for f in json.load(r)}
    return [{"name": n, "size": listing.get(n), "url": VED_BASE + urllib.request.quote(n), "md5": None}
            for n in VED_FILES]


def download(item: dict, dest_dir: Path):
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / item["name"]
    if dest.exists() and dest.stat().st_size == item["size"]:
        print(f"  exists  {dest.name}")
        return
    md5 = hashlib.md5()
    with urllib.request.urlopen(item["url"]) as r, open(dest, "wb") as f:
        while chunk := r.read(1 << 20):
            f.write(chunk)
            md5.update(chunk)
    if item["md5"] and md5.hexdigest() != item["md5"]:
        dest.unlink()
        sys.exit(f"checksum mismatch for {item['name']}")
    print(f"  fetched {dest.name} ({dest.stat().st_size / 1e6:.1f} MB) md5={md5.hexdigest()}")


def extract_ved():
    """Unpack the VED weekly CSVs into data/raw/ved/csv/ (refuses archive paths that escape it)."""
    import py7zr

    out = RAW / "ved" / "csv"
    out.mkdir(parents=True, exist_ok=True)
    for name in VED_FILES[:2]:
        with py7zr.SevenZipFile(RAW / "ved" / name) as z:
            for n in z.getnames():
                target = (out / n).resolve()
                if out.resolve() not in target.parents:
                    sys.exit(f"unsafe path in archive: {n}")
            z.extractall(out)
    print(f"  extracted {len(list(out.rglob('*.csv')))} CSV files into {out}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--yes", action="store_true", help="confirm the download")
    ap.add_argument("--extract-ved", action="store_true", help="unpack already-downloaded VED archives")
    ap.add_argument("which", nargs="?", default="vedzenodo", choices=["ved", "zenodo", "vedzenodo"])
    args = ap.parse_args()
    if args.extract_ved:
        extract_ved()
        return
    sets = []
    if args.which in ("ved", "vedzenodo"):
        sets.append(("ved", ved_files()))
    if args.which in ("zenodo", "vedzenodo"):
        sets.append(("zenodo_can", zenodo_files()))
    for name, files in sets:
        total = sum(f["size"] or 0 for f in files) / 1e6
        print(f"{name}: {len(files)} files, {total:.0f} MB")
        for f in files:
            print(f"  {f['name']:<36} {(f['size'] or 0) / 1e6:8.1f} MB")
    if args.list or not args.yes:
        if not args.list:
            print("\nNothing downloaded. Re-run with --yes to download.")
        return
    for name, files in sets:
        for f in files:
            download(f, RAW / name)


if __name__ == "__main__":
    main()
