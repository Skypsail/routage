"""Relais GFS longue portée (NOAA) pour le plugin de routage.

Exécuté par GitHub Actions (voir .github/workflows/gfs.yml) toutes les 6 h :
  1. cherche le dernier run GFS complet (jusqu'à +384 h = 16 jours) sur NOMADS ;
  2. télécharge le vent à 10 m (UGRD/VGRD) sur l'Atlantique, grille 1°, pas de 3 h ;
  3. écrit gfs/gfs.json (description) et gfs/gfs.bin (données compactes) ;
  4. télécharge aussi les vagues GFS-Wave (WAVEWATCH III) du même run : hauteur significative,
     direction et période de la houle principale, même grille, même pas de temps ;
     écrit gfs/waves.json et gfs/waves.bin (si les vagues échouent, le vent est publié quand même).

Format de gfs.bin : entiers signés 8 bits, ordre [échéance][u puis v][latitude][longitude],
valeur en nœuds = octet × scale (0,5). u > 0 : vent portant vers l'est ; v > 0 : vers le nord.

Format de waves.bin : octets non signés, ordre [échéance][hs, dir, per][latitude][longitude] ;
hs (m) = octet × 0,1 ; dir (d'où vient la houle, degrés) = octet × 2 ; per (s) = octet × 0,1 ;
255 = pas de valeur (terre).
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import eccodes

OUT = Path(__file__).resolve().parent.parent / "gfs"

# Zone couverte : Atlantique Nord (Manche → Antilles)
LAT_MIN, LAT_MAX = 0, 60
LON_MIN, LON_MAX = -85, 10
STEP = 1.0
HOURS = list(range(0, 385, 3))
SCALE = 0.5  # nœuds par unité
MS_TO_KT = 1.943844

BASE = "https://nomads.ncep.noaa.gov"
UA = {"User-Agent": "windy-routage-relay (github.com/Skypsail/routage)"}


def http_get(url: str, tries: int = 4) -> bytes:
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except (urllib.error.URLError, TimeoutError) as err:
            if isinstance(err, urllib.error.HTTPError) and err.code == 404:
                raise
            wait = 5 * (k + 1)
            print(f"  nouvel essai dans {wait} s ({err})")
            time.sleep(wait)
    raise RuntimeError(f"échec : {url}")


def exists(url: str) -> bool:
    try:
        req = urllib.request.Request(url, headers=UA, method="HEAD")
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status == 200
    except urllib.error.URLError:
        return False


def latest_complete_cycle() -> datetime:
    """Dernier run dont l'échéance +384 h est publiée."""
    now = datetime.now(timezone.utc)
    cycle = now.replace(minute=0, second=0, microsecond=0, hour=(now.hour // 6) * 6)
    for _ in range(6):
        d, h = cycle.strftime("%Y%m%d"), cycle.strftime("%H")
        url = f"{BASE}/pub/data/nccf/com/gfs/prod/gfs.{d}/{h}/atmos/gfs.t{h}z.pgrb2.1p00.f384"
        if exists(url):
            return cycle
        print(f"Run {d} {h}z incomplet")
        cycle -= timedelta(hours=6)
    raise SystemExit("Aucun run GFS complet trouvé")


def filter_url(cycle: datetime, fh: int) -> str:
    d, h = cycle.strftime("%Y%m%d"), cycle.strftime("%H")
    q = {
        "file": f"gfs.t{h}z.pgrb2.1p00.f{fh:03d}",
        "lev_10_m_above_ground": "on",
        "var_UGRD": "on",
        "var_VGRD": "on",
        "subregion": "",
        "leftlon": LON_MIN,
        "rightlon": LON_MAX,
        "toplat": LAT_MAX,
        "bottomlat": LAT_MIN,
        "dir": f"/gfs.{d}/{h}/atmos",
    }
    return f"{BASE}/cgi-bin/filter_gfs_1p00.pl?{urllib.parse.urlencode(q)}"


def decode(grib: bytes, n_lat: int, n_lon: int) -> dict[str, list[float]]:
    """Renvoie {'u': [...], 'v': [...]} en nœuds, rangés [lat][lon] du sud au nord, d'ouest en est."""
    out: dict[str, list[float]] = {}
    pos = 0
    while pos < len(grib):
        start = grib.find(b"GRIB", pos)
        if start < 0:
            break
        length = int.from_bytes(grib[start + 8 : start + 16], "big")  # GRIB2 : longueur totale
        gid = eccodes.codes_new_from_message(grib[start : start + length])
        pos = start + length
        try:
            name = eccodes.codes_get(gid, "shortName")
            comp = {"10u": "u", "10v": "v"}.get(name)
            if not comp:
                continue
            values = eccodes.codes_get_values(gid)
            lats = eccodes.codes_get_array(gid, "latitudes")
            lons = eccodes.codes_get_array(gid, "longitudes")
        finally:
            eccodes.codes_release(gid)
        grid = [float("nan")] * (n_lat * n_lon)
        for val, la, lo in zip(values, lats, lons):
            lo = ((lo + 180) % 360) - 180
            i = round((la - LAT_MIN) / STEP)
            j = round((lo - LON_MIN) / STEP)
            if 0 <= i < n_lat and 0 <= j < n_lon:
                grid[i * n_lon + j] = val * MS_TO_KT
        out[comp] = grid
    if "u" not in out or "v" not in out:
        raise ValueError("u ou v manquant dans le fichier GRIB")
    return out


# ---------------------------------------------------------------- Vagues (GFS-Wave)

# (discipline, catégorie, paramètre) GRIB2 → nom court
WAVE_PARAMS = {(10, 0, 3): "hs", (10, 0, 10): "dir", (10, 0, 11): "per"}
WAVE_SCALE = {"hs": 0.1, "dir": 2.0, "per": 0.1}


def wave_filter_url(cycle: datetime, fh: int, left: float, right: float) -> str:
    d, h = cycle.strftime("%Y%m%d"), cycle.strftime("%H")
    q = {
        "file": f"gfswave.t{h}z.global.0p25.f{fh:03d}.grib2",
        "var_HTSGW": "on",
        "var_DIRPW": "on",
        "var_PERPW": "on",
        "subregion": "",
        "leftlon": left,
        "rightlon": right,
        "toplat": LAT_MAX,
        "bottomlat": LAT_MIN,
        "dir": f"/gfs.{d}/{h}/wave/gridded",
    }
    return f"{BASE}/cgi-bin/filter_gfswave.pl?{urllib.parse.urlencode(q)}"


def decode_waves(grib: bytes, n_lat: int, n_lon: int) -> dict[str, list[float]]:
    """Grille 0,25° → points des degrés entiers ; NaN sur terre.
    Plusieurs messages d'un même paramètre (zones découpées) sont fusionnés."""
    out: dict[str, list[float]] = {}
    pos = 0
    while pos < len(grib):
        start = grib.find(b"GRIB", pos)
        if start < 0:
            break
        length = int.from_bytes(grib[start + 8 : start + 16], "big")
        gid = eccodes.codes_new_from_message(grib[start : start + length])
        pos = start + length
        try:
            key = (
                eccodes.codes_get(gid, "discipline"),
                eccodes.codes_get(gid, "parameterCategory"),
                eccodes.codes_get(gid, "parameterNumber"),
            )
            comp = WAVE_PARAMS.get(key)
            if not comp:
                print(f"  paramètre de vague inattendu {key} ({eccodes.codes_get(gid, 'shortName')})")
                continue
            missing = eccodes.codes_get(gid, "missingValue")
            values = eccodes.codes_get_values(gid)
            lats = eccodes.codes_get_array(gid, "latitudes")
            lons = eccodes.codes_get_array(gid, "longitudes")
        finally:
            eccodes.codes_release(gid)
        grid = out.setdefault(comp, [float("nan")] * (n_lat * n_lon))
        for val, la, lo in zip(values, lats, lons):
            lo = ((lo + 180) % 360) - 180
            # On ne garde que les degrés entiers (grille 1°). Tolérance large : la grille des vagues
            # NOAA est décalée de quelques millionièmes de degré (ex. 275,000012°).
            if abs(la - round(la)) > 1e-3 or abs(lo - round(lo)) > 1e-3:
                continue
            i = round((la - LAT_MIN) / STEP)
            j = round((lo - LON_MIN) / STEP)
            if 0 <= i < n_lat and 0 <= j < n_lon and val != missing:
                grid[i * n_lon + j] = float(val)
    for comp in ("hs", "dir", "per"):
        if comp not in out:
            raise ValueError(f"{comp} manquant dans le fichier GRIB des vagues")
    return out


def to_uint8(x: float, scale: float) -> int:
    if x != x:  # NaN : terre
        return 255
    return max(0, min(254, round(x / scale)))


def fetch_waves(cycle: datetime, n_lat: int, n_lon: int, wind_times: list[int]) -> None:
    """Télécharge les vagues du run et écrit waves.bin / waves.json (aux mêmes échéances que le vent)."""
    data = bytearray()
    times: list[int] = []
    for fh in HOURS:
        t = int((cycle + timedelta(hours=fh)).timestamp() * 1000)
        if t not in wind_times:
            break
        try:
            w = decode_waves(http_get(wave_filter_url(cycle, fh, LON_MIN, LON_MAX)), n_lat, n_lon)
        except Exception as err:  # noqa: BLE001
            print(f"Vagues +{fh} h illisibles ({err}) : arrêt ici")
            break
        for comp in ("hs", "dir", "per"):
            data.extend(to_uint8(x if comp != "dir" else x % 360, WAVE_SCALE[comp]) for x in w[comp])
        times.append(t)
        if fh % 48 == 0:
            ok = sum(1 for x in w["hs"] if x == x)
            print(f"  vagues +{fh} h OK ({ok} points en mer sur {n_lat * n_lon})")
            if fh == 0 and ok < 0.4 * n_lat * n_lon:
                raise ValueError(f"trop peu de points de vagues ({ok}) : grille incomplète")
        time.sleep(0.6)
    if len(times) < 40:
        print(f"Vagues : seulement {len(times)} échéances, non publiées")
        return
    (OUT / "waves.bin").write_bytes(bytes(data))
    meta = {
        "model": "GFS-Wave 0,25° → 1° (NOAA WAVEWATCH III)",
        "ref": int(cycle.timestamp() * 1000),
        "lat0": LAT_MIN,
        "lon0": LON_MIN,
        "step": STEP,
        "nLat": n_lat,
        "nLon": n_lon,
        "scale": WAVE_SCALE,
        "ts": times,
    }
    (OUT / "waves.json").write_text(json.dumps(meta), encoding="utf-8")
    print(f"waves.bin : {len(data) // 1024} Ko, {len(times)} échéances")


def to_int8(x: float) -> int:
    if x != x:  # NaN
        return 0
    return max(-127, min(127, round(x / SCALE))) & 0xFF


def main() -> None:
    n_lat = int((LAT_MAX - LAT_MIN) / STEP) + 1
    n_lon = int((LON_MAX - LON_MIN) / STEP) + 1
    cycle = latest_complete_cycle()
    # Run déjà publié ? (heure du run publié passée par la tâche GitHub)
    published = int(os.environ.get("PUBLISHED_REF", "0") or 0)
    waves_published = os.environ.get("PUBLISHED_WAVES", "0") == "1"
    if int(cycle.timestamp() * 1000) <= published and waves_published:
        print(f"Run {cycle:%Y-%m-%d %H}z déjà publié : rien à faire")
        sys.exit(0)
    print(f"Run GFS {cycle:%Y-%m-%d %H}z, grille {n_lat}×{n_lon}, {len(HOURS)} échéances")

    data = bytearray()
    times: list[int] = []
    for fh in HOURS:
        try:
            uv = decode(http_get(filter_url(cycle, fh)), n_lat, n_lon)
        except Exception as err:  # noqa: BLE001
            print(f"Échéance +{fh} h illisible ({err}) : arrêt ici")
            break
        for comp in ("u", "v"):
            data.extend(to_int8(x) for x in uv[comp])
        times.append(int((cycle + timedelta(hours=fh)).timestamp() * 1000))
        if fh % 48 == 0:
            print(f"  +{fh} h OK")
        time.sleep(0.6)  # NOMADS limite le nombre de requêtes par minute

    if len(times) < 40:  # moins de 5 jours : on garde l'ancienne version
        sys.exit(f"Seulement {len(times)} échéances récupérées : publication annulée")

    OUT.mkdir(exist_ok=True)
    (OUT / "gfs.bin").write_bytes(bytes(data))
    meta = {
        "model": "GFS 1° (NOAA)",
        "ref": int(cycle.timestamp() * 1000),
        "lat0": LAT_MIN,
        "lon0": LON_MIN,
        "step": STEP,
        "nLat": n_lat,
        "nLon": n_lon,
        "scale": SCALE,
        "ts": times,
    }
    print(f"gfs.bin : {len(data) // 1024} Ko, jusqu'à +{HOURS[len(times) - 1]} h")

    # Vagues du même run (facultatif : le vent est publié même si elles échouent)
    try:
        fetch_waves(cycle, n_lat, n_lon, times)
    except Exception as err:  # noqa: BLE001
        print(f"Vagues non récupérées : {err}")
    meta["waves"] = (OUT / "waves.bin").exists()
    (OUT / "gfs.json").write_text(json.dumps(meta), encoding="utf-8")


if __name__ == "__main__":
    main()
