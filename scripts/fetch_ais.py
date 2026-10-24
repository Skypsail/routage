"""Relais AIS (aisstream.io) pour le plugin de routage.

Exécuté par GitHub Actions (voir .github/workflows/ais.yml) :
  1. reste connecté au flux aisstream.io pendant RUN_MINUTES (reconnexion automatique) ;
  2. garde la dernière position de chaque navire (+ nom et type s'ils sont connus) ;
  3. toutes les PUBLISH_EVERY secondes, écrit ais/ais.json (navires vus depuis moins de
     MAX_AGE_H heures) et le publie sur la branche « ais ».

La clé aisstream.io est lue dans la variable d'environnement AISSTREAM_KEY (secret GitHub).
AIS gratuit = réception terrestre : environ 20 à 40 milles autour des côtes, rien au grand large.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "ais"
PREVIOUS = ROOT / "ais-previous" / "ais.json"  # relevé précédent (branche ais), s'il existe

URL = "wss://stream.aisstream.io/v0/stream"
RUN_MINUTES = float(os.environ.get("RUN_MINUTES", "340"))  # durée d'un lancement (GitHub : 6 h max)
PUBLISH_EVERY = float(os.environ.get("PUBLISH_EVERY", "150"))  # publication toutes les 2 min 30
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


async def listen_forever(key: str, vessels: dict[int, dict], stats: dict, stop: float) -> None:
    """Reste connecté jusqu'à l'heure stop ; se reconnecte en cas de coupure."""
    delay = 5
    while time.monotonic() < stop:
        try:
            async with websockets.connect(URL, open_timeout=20, max_size=2**22, ping_interval=30) as ws:
                await ws.send(json.dumps({
                    "APIKey": key,
                    "BoundingBoxes": list(ZONES.values()),
                    "FilterMessageTypes": list(POSITION_TYPES + STATIC_TYPES),
                }))
                print("Connecté à aisstream.io")
                delay = 5
                while time.monotonic() < stop:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=min(60, max(0.1, stop - time.monotonic())))
                    except asyncio.TimeoutError:
                        continue
                    msg = json.loads(raw)
                    if "error" in msg:
                        raise RuntimeError(f"aisstream.io : {msg['error']}")
                    apply_message(msg, vessels)
                    stats["messages"] += 1
        except RuntimeError:
            raise  # clé refusée, etc. : inutile d'insister
        except Exception as err:  # noqa: BLE001 — coupure réseau : on se reconnecte
            print(f"Connexion perdue ({err}), reconnexion dans {delay} s")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 120)


def snapshot(vessels: dict[int, dict]) -> dict:
    limit = (time.time() - MAX_AGE_H * 3600) * 1000
    kept = [v for v in vessels.values() if "lat" in v and v.get("t", 0) >= limit]
    kept.sort(key=lambda v: v["mmsi"])
    return {"updated": int(time.time() * 1000), "zones": ZONES, "vessels": kept}


def publish(data: dict) -> None:
    """Écrit ais/ais.json et le pousse (un seul commit, historique écrasé) sur la branche ais."""
    OUT_DIR.mkdir(exist_ok=True)
    (OUT_DIR / "ais.json").write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    remote = os.environ.get("PUSH_URL")
    if not remote:
        return  # essai local : pas de publication
    git = ["git", "-C", str(OUT_DIR)]
    subprocess.run(["rm", "-rf", str(OUT_DIR / ".git")], check=True)
    subprocess.run(git + ["init", "-q", "-b", "ais"], check=True)
    subprocess.run(git + ["add", "ais.json"], check=True)
    subprocess.run(git + ["-c", "user.name=ais-bot", "-c", "user.email=ais-bot@users.noreply.github.com",
                          "commit", "-q", "-m", f"AIS {time.strftime('%d/%m %H:%M', time.gmtime())} UTC"], check=True)
    r = subprocess.run(git + ["push", "-f", "-q", remote, "ais"], capture_output=True, text=True)
    if r.returncode:
        print("Publication échouée :", r.stderr.strip().replace(remote, "***"))


async def run(key: str, vessels: dict[int, dict]) -> None:
    stats = {"messages": 0}
    stop = time.monotonic() + RUN_MINUTES * 60
    listener = asyncio.create_task(listen_forever(key, vessels, stats, stop))
    while not listener.done():
        await asyncio.wait({listener}, timeout=PUBLISH_EVERY)
        data = snapshot(vessels)
        await asyncio.to_thread(publish, data)
        print(f"{time.strftime('%H:%M:%S')} · {stats['messages']} messages · {len(data['vessels'])} navires publiés")
    listener.result()  # remonte une éventuelle erreur (clé refusée…)


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
    print(f"{len(vessels)} navires repris du relevé précédent")
    asyncio.run(run(key, vessels))


if __name__ == "__main__":
    main()
