"""Relais FTP -> GitHub Pages pour les positions de la Route du Rhum 2026.

Exécuté par GitHub Actions (voir .github/workflows/positions.yml) :
  1. se connecte au FTP de la course (identifiants dans les secrets GitHub) ;
  2. télécharge pos.txt (positions) et les fichiers d'identifiants des bateaux ;
  3. ajoute les nouvelles positions à l'historique (data/history.json) ;
  4. écrit positions.json, lu par le plugin Windy.

Format de pos.txt :  classement;numéro;latitude;longitude;date heure;cap/vitesse
  - date : mm/jj/aa (format américain, utilisé depuis le 29/10) ou jj/mm/aa — les deux sont acceptés ;
  - cap/vitesse (facultatif) : « 109/0.0 » = route fond 109°, vitesse fond 0,0 nœud.
Les coordonnées sont en DEGRÉS.MINUTES : 48.3900N = 48°39,00' N (et non 48,39°).
"""

from __future__ import annotations

import ftplib
import io
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HISTORY = ROOT / "data" / "history.json"
OUTPUT = ROOT / "positions.json"

# Les traces ne commencent qu'au départ de la course (on ignore les positions de test / au ponton)
TRACK_FROM_MS = int(datetime(2026, 11, 1, tzinfo=timezone.utc).timestamp() * 1000)

ID_DIR = "Adrena_dcc/ID bateaux"
ID_FILES = {
    "Id_Ultim.txt": "Ultim",
    "Id_Ocean50.txt": "Ocean Fifty",
    "Id_IMOCA.txt": "IMOCA",
    "Id_CLASS40.txt": "Class40",
    "Id_Vintage MULTI.txt": "Vintage Multi",
    "Id_Vintage Mono.txt": "Vintage Mono",
}


# ---------------------------------------------------------------- FTP

def ftp_connect() -> ftplib.FTP:
    """Essaie FTP1 puis FTP2."""
    user = os.environ["FTP_USER"]
    password = os.environ["FTP_PASSWORD"]
    hosts = [h.strip() for h in os.environ["FTP_HOSTS"].split(",") if h.strip()]
    last_error: Exception | None = None
    for host in hosts:
        try:
            ftp = ftplib.FTP(host, timeout=30)
            ftp.login(user, password)
            print(f"Connecté à {host}")
            return ftp
        except Exception as err:  # noqa: BLE001
            print(f"Échec {host} : {err}")
            last_error = err
    raise SystemExit(f"Aucun serveur FTP joignable ({last_error})")


def ftp_read(ftp: ftplib.FTP, path: str) -> str:
    buf = io.BytesIO()
    ftp.retrbinary(f"RETR {path}", buf.write)
    raw = buf.getvalue()
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace")


# ---------------------------------------------------------------- Lecture

def parse_ids(text: str, cls: str) -> dict[int, dict]:
    """Ligne : numéro <tab> NOM Prénom <tabs> NOM DU BATEAU (tabulations irrégulières)."""
    boats: dict[int, dict] = {}
    for line in text.splitlines():
        cells = [c.strip() for c in line.split("\t") if c.strip()]
        if len(cells) < 2 or not cells[0].isdigit():
            continue
        boat_id = int(cells[0])
        boat = cells[-1] if len(cells) >= 3 else ""
        skipper = " ".join(" ".join(cells[1:-1] if len(cells) >= 3 else cells[1:]).split())
        boats[boat_id] = {"id": boat_id, "cls": cls, "skipper": skipper, "boat": " ".join(boat.split())}
    return boats


def ddmm_to_deg(value: str) -> float:
    """'48.3900N' -> 48.65 ; '2.0050W' -> -2.00833 (degrés.minutes)."""
    value = value.strip().upper()
    hemi = value[-1]
    num = float(value[:-1])
    deg = int(num)
    minutes = (num - deg) * 100
    out = deg + minutes / 60
    return -out if hemi in ("S", "W") else out


def parse_date(raw: str) -> datetime:
    """Accepte mm/jj/aa et jj/mm/aa ; en cas d'ambiguïté (ex. 11/05/26), garde la date la plus proche d'aujourd'hui."""
    now = datetime.now(timezone.utc)
    candidates = []
    for fmt in ("%m/%d/%y %H:%M:%S", "%d/%m/%y %H:%M:%S"):
        try:
            candidates.append(datetime.strptime(raw.strip(), fmt).replace(tzinfo=timezone.utc))
        except ValueError:
            pass
    if not candidates:
        raise ValueError(f"date illisible « {raw} »")
    return min(candidates, key=lambda d: abs((d - now).total_seconds()))


def parse_cog_sog(raw: str) -> tuple[int | None, float | None]:
    """'109/0.0' -> (109, 0.0)."""
    try:
        cog, sog = raw.split("/")
        return round(float(cog)) % 360, round(float(sog), 1)
    except ValueError:
        return None, None


def parse_positions(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        cells = [c.strip() for c in line.split(";")]
        if len(cells) < 5 or not cells[1].isdigit():
            continue
        try:
            t = parse_date(cells[4])
            cog, sog = parse_cog_sog(cells[5]) if len(cells) > 5 and "/" in cells[5] else (None, None)
            out.append({
                "rank": int(cells[0]) if cells[0].isdigit() else None,
                "id": int(cells[1]),
                "lat": round(ddmm_to_deg(cells[2]), 5),
                "lon": round(ddmm_to_deg(cells[3]), 5),
                "t": int(t.timestamp() * 1000),
                "cog": cog,
                "sog": sog,
            })
        except ValueError as err:
            print(f"Ligne ignorée ({err}) : {line}")
    return out


# ---------------------------------------------------------------- Calculs

def distance_nm(lat1, lon1, lat2, lon2) -> float:
    r = math.radians
    d_lat, d_lon = r(lat2 - lat1), r(lon2 - lon1)
    h = math.sin(d_lat / 2) ** 2 + math.cos(r(lat1)) * math.cos(r(lat2)) * math.sin(d_lon / 2) ** 2
    return 2 * 3440.065 * math.asin(min(1, math.sqrt(h)))


def bearing(lat1, lon1, lat2, lon2) -> float:
    r = math.radians
    y = math.sin(r(lon2 - lon1)) * math.cos(r(lat2))
    x = math.cos(r(lat1)) * math.sin(r(lat2)) - math.sin(r(lat1)) * math.cos(r(lat2)) * math.cos(r(lon2 - lon1))
    return (math.degrees(math.atan2(y, x)) + 360) % 360


# ---------------------------------------------------------------- Principal

def main() -> None:
    ftp = ftp_connect()
    boats: dict[int, dict] = {}
    for name, cls in ID_FILES.items():
        try:
            boats.update(parse_ids(ftp_read(ftp, f"{ID_DIR}/{name}"), cls))
        except ftplib.all_errors as err:
            print(f"Fichier d'identifiants illisible {name} : {err}")
    positions = parse_positions(ftp_read(ftp, "pos.txt"))
    ftp.quit()
    print(f"{len(boats)} bateaux connus, {len(positions)} positions lues")
    if not positions:
        sys.exit("pos.txt vide ou illisible : rien à faire")

    # Historique : { "id": [[t, lat, lon], ...] }
    history: dict[str, list[list[float]]] = {}
    if HISTORY.exists():
        history = json.loads(HISTORY.read_text(encoding="utf-8"))
    # Purge des points d'avant le départ (données de test du FTP)
    history = {k: [pt for pt in v if pt[0] >= TRACK_FROM_MS] for k, v in history.items()}
    added = 0
    for p in positions:
        track = history.setdefault(str(p["id"]), [])
        if p["t"] < TRACK_FROM_MS:
            continue  # position d'avant-course : publiée mais pas ajoutée à la trace
        if track and track[-1][0] >= p["t"]:
            continue  # position déjà connue
        track.append([p["t"], p["lat"], p["lon"]])
        added += 1
    print(f"{added} nouvelle(s) position(s) ajoutée(s) aux traces")
    previous_fix = None
    if OUTPUT.exists():
        try:
            previous_fix = json.loads(OUTPUT.read_text(encoding="utf-8")).get("lastFix")
        except ValueError:
            pass
    if added == 0 and previous_fix == max(p["t"] for p in positions):
        print("Rien de nouveau : fichiers inchangés")
        return

    out_boats = []
    for p in positions:
        info = boats.get(p["id"], {"id": p["id"], "cls": "?", "skipper": "", "boat": f"N°{p['id']}"})
        track = history.get(str(p["id"]), [])
        # Cap et vitesse fournis par la balise ; sinon calculés entre les 2 dernières positions
        sog, cog = p["sog"], p["cog"]
        if sog is None and len(track) >= 2:
            (t1, la1, lo1), (t2, la2, lo2) = track[-2], track[-1]
            hours = (t2 - t1) / 3600e3
            if hours > 0:
                sog = round(distance_nm(la1, lo1, la2, lo2) / hours, 1)
                cog = round(bearing(la1, lo1, la2, lo2))
        out_boats.append({**info, "rank": p["rank"], "lat": p["lat"], "lon": p["lon"], "t": p["t"],
                          "sog": sog, "cog": cog})

    data = {
        "updated": int(datetime.now(timezone.utc).timestamp() * 1000),
        "lastFix": max(p["t"] for p in positions),
        "boats": out_boats,
        "tracks": history,
    }
    HISTORY.parent.mkdir(exist_ok=True)
    HISTORY.write_text(json.dumps(history, separators=(",", ":")), encoding="utf-8")
    OUTPUT.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"positions.json écrit ({OUTPUT.stat().st_size // 1024} Ko)")


if __name__ == "__main__":
    main()
