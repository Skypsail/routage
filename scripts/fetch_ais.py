"""Relais AIS (aisstream.io) pour le plugin de routage.

Exécuté par GitHub Actions (voir .github/workflows/ais.yml) toutes les 15 min :
  1. se connecte au flux aisstream.io pendant LISTEN_SECONDS sur les zones ZONES ;
  2. garde la dernière position de chaque navire (+ nom et type s'ils sont connus) ;
  3. fusionne avec le relevé précédent (les navires vus depuis moins de MAX_AGE_H heures) ;
  4. écrit ais/ais.json, publié sur la branche « ais ».

La clé aisstream.io est lue dans la variable d'environnement AISSTREAM_KEY (secret GitHub).
AIS gratuit = réception terrestre : environ 20 à 40 milles autour des côtes, rien au grand large.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "ais"
PREVIOUS = ROOT / "ais-previous" / "ais.json"  # relevé précédent (branche ais), s'il existe

URL = "wss://stream.aisstream.io/v0/stream"
LISTEN_SECONDS = 75
MAX_AGE_H = 6

# Zones surveillées : [[lat_min, lon_min], [lat_max, lon_max]] — à adapter librement
ZONES = {
    "Manche Ouest et Bretagne": [[47.0, -7.5], [50.5, -1.0]],
    "Golfe de Gascogne": [[43.3, -10.0], [47.0, -1.0]],
    "Guadeloupe": [[15.6, -62.3], [16.9, -60.8]],
}

POSITION_TYPES = ("PositionReport", "StandardClassBPositionReport", "ExtendedClassBPositionReport")
STATIC_TYPES = ("ShipStaticData", "StaticDataReport")


def parse_time(meta: dict) -> int:
    """'2026-11-01 13:02:11.123456 +0000 UTC' -> ms."""
    raw = str(meta.get("time_utc", ""))
    try:
        dt = datetime.strptime(raw[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except ValueError:
        return int(time.time() * 1000)


def clean_name(name) -> str:
    return " ".join(str(name or "").replace("@", " ").split())


def apply_message(msg: dict, vessels: dict[int, dict]) -> None:
    mtype = msg.get("MessageType")
    meta = msg.get("MetaData") or {}
    body = (msg.get("Message") or {}).get(mtype) or {}
    mmsi = meta.get("MMSI") or body.get("UserID")
    if not mmsi:
        return
    mmsi = int(mmsi)
    v = vessels.setdefault(mmsi, {"mmsi": mmsi})
    name = clean_name(meta.get("ShipName"))
    if name:
        v["name"] = name

    if mtype in POSITION_TYPES:
        lat = body.get("Latitude", meta.get("latitude"))
        lon = body.get("Longitude", meta.get("longitude"))
        if lat is None or lon is None or abs(lat) > 90 or abs(lon) > 180:
            return
        t = parse_time(meta)
        if t < v.get("t", 0):
            return  # message plus ancien que celui déjà connu
        sog = body.get("Sog")
        cog = body.get("Cog")
        hdg = body.get("TrueHeading")
        v.update({
            "lat": round(float(lat), 5),
            "lon": round(float(lon), 5),
            "t": t,
            "sog": round(float(sog), 1) if sog is not None and sog < 102.2 else None,
            "cog": round(float(cog)) if cog is not None and cog < 360 else None,
            "hdg": int(hdg) if hdg is not None and hdg < 360 else None,
        })
        if mtype == "ExtendedClassBPositionReport":
            if body.get("Type"):
                v["type"] = int(body["Type"])
            if clean_name(body.get("Name")):
                v["name"] = clean_name(body.get("Name"))
    elif mtype == "ShipStaticData":
        if body.get("Type"):
            v["type"] = int(body["Type"])
        if clean_name(body.get("Name")):
            v["name"] = clean_name(body.get("Name"))
    elif mtype == "StaticDataReport":
        a = body.get("ReportA") or {}
        b = body.get("ReportB") or {}
        if a.get("Valid") and clean_name(a.get("Name")):
            v["name"] = clean_name(a.get("Name"))
        if b.get("Valid") and b.get("ShipType"):
            v["type"] = int(b["ShipType"])


async def listen(key: str, vessels: dict[int, dict]) -> int:
    count = 0
    async with websockets.connect(URL, open_timeout=20, max_size=2**22) as ws:
        await ws.send(json.dumps({
            "APIKey": key,
            "BoundingBoxes": list(ZONES.values()),
            "FilterMessageTypes": list(POSITION_TYPES + STATIC_TYPES),
        }))
        end = time.monotonic() + LISTEN_SECONDS
        while time.monotonic() < end:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, end - time.monotonic()))
            except asyncio.TimeoutError:
                break
            msg = json.loads(raw)
            if "error" in msg:
                raise RuntimeError(f"aisstream.io : {msg['error']}")
            apply_message(msg, vessels)
            count += 1
    return count


def main() -> None:
    key = os.environ.get("AISSTREAM_KEY", "").strip()
    if not key:
        sys.exit("Secret AISSTREAM_KEY absent")

    # Relevé précédent : garde noms/types et les navires encore récents
    vessels: dict[int, dict] = {}
    if PREVIOUS.exists():
        try:
            for v in json.loads(PREVIOUS.read_text(encoding="utf-8")).get("vessels", []):
                vessels[int(v["mmsi"])] = v
        except (ValueError, KeyError) as err:
            print(f"Relevé précédent illisible : {err}")
    before = len(vessels)

    count = asyncio.run(listen(key, vessels))
    print(f"{count} messages AIS reçus en {LISTEN_SECONDS} s")

    limit = (time.time() - MAX_AGE_H * 3600) * 1000
    kept = [v for v in vessels.values() if "lat" in v and v.get("t", 0) >= limit]
    kept.sort(key=lambda v: v["mmsi"])
    print(f"{len(kept)} navires publiés (précédent : {before})")

    OUT_DIR.mkdir(exist_ok=True)
    data = {
        "updated": int(time.time() * 1000),
        "zones": ZONES,
        "vessels": kept,
    }
    (OUT_DIR / "ais.json").write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"ais.json : {(OUT_DIR / 'ais.json').stat().st_size // 1024} Ko")


if __name__ == "__main__":
    main()
