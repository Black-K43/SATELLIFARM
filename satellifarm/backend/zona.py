"""
zona.py — Builds the zone JSON (the "contract" the web page reads) for one point.

  python zona.py 42.03 -93.62          -> prints the JSON (useful to test / precompute demo zones)

Flow:  every source is tried; if it fails, a backup is used and the failure is written
in "fuentes" so the page can show it under "What we don't know".
NASA POWER is the backbone (global, no key): if POWER fails there is no zone.
"""
import sys
import json
import logging
import datetime as dt

import fuentes as F
from calculos import (et0_penman_monteith_mensual, idw, fusion_inversa_varianza, sigma_estaciones, DIAS_MES)

log = logging.getLogger("zona")
CODIGOS = ("sin_clave", "sin_datos", "sin_estaciones", "datos_incompletos")


def _intentar(estado, nombre, fn, *args, **kw):
    """Run a source; record 'ok' or the reason it failed. Never raises."""
    try:
        res = fn(*args, **kw)
        estado.setdefault(nombre, "ok")
        return res
    except RuntimeError as e:
        estado[nombre] = str(e) if str(e) in CODIGOS else "error"
    except Exception as e:                                       # network, parsing, auth...
        log.warning("%s failed: %s", nombre, e)
        estado[nombre] = "error"
    return None


def _normal_ventana(normales_mensuales, ini, fin):
    """Expected rain between two dates from monthly normals (pro-rated by days)."""
    total, d = 0.0, ini
    while d < fin:
        total += normales_mensuales[d.month - 1] / DIAS_MES[d.month - 1]
        d += dt.timedelta(days=1)
    return total


def _helada_aprox(tmin):
    """Backup if POWER daily fails: rough frost days from the monthly mean minimum."""
    return [round(min(1, max(0, (3 - t) / 8)) * DIAS_MES[i]) for i, t in enumerate(tmin)]


VERANO = (5, 6, 7)          # Jun, Jul, Aug (0-based)

def detectar_riego(et_real, pr_gridmet, lluvia, et0, ndvi_mes, umbral_mm=75):
    """Is the field irrigated? Water balance logic:
    if the field evaporated + transpired in summer MORE than the rain that fell, the difference had to
    come from somewhere. The soil can carry ~50–100 mm from spring, so above ~75 mm extra -> irrigation.
    1st choice: OpenET actual ET (30 m, the field itself). Backup: green NDVI in summer while rain covers
    less than half of ET0 (a crop that stays green with so little rain is being watered)."""
    if et_real:
        ll = pr_gridmet or lluvia
        etv, llv = sum(et_real[m] for m in VERANO), sum(ll[m] for m in VERANO)
        return {"probable": (etv - llv) > umbral_mm, "etVerano": round(etv), "lluviaVerano": round(llv), "fuente": "OpenET"}
    if ndvi_mes and all(ndvi_mes[m] is not None for m in VERANO):
        ndv = sum(ndvi_mes[m] for m in VERANO) / 3
        llv, etv = sum(lluvia[m] for m in VERANO), sum(et0[m] for m in VERANO)
        return {"probable": ndv >= 0.6 and llv < 0.5 * etv, "etVerano": round(etv), "lluviaVerano": round(llv),
                "ndviVerano": round(ndv, 2), "fuente": "NDVI + ET0"}
    return None


def construir_zona(lat, lon, carpeta_capas="capas"):
    fu = {}                                     # status of every source
    r1 = lambda x: round(x, 1) if x is not None else None

    # --- 0) where is it -------------------------------------------------------
    info = None
    try:
        info = F.condado(lat, lon)
    except Exception as e:
        log.warning("FCC failed: %s", e)

    # --- 1) climate backbone: NASA POWER --------------------------------------
    pw = F.power_climatologia(lat, lon)        # if this fails -> exception -> page falls back to demo
    fu["power"] = "ok"
    diario = _intentar(fu, "power", F.dias_helada_y_reciente, lat, lon)
    if diario is None:
        fu["power"] = "parcial"

    # --- 2) water: OpenET (ETo, actual ET, gridMET rain) ----------------------
    eto_openet = _intentar(fu, "openet", F.openet_mensual, lat, lon, "eto")
    et_real = pr_gridmet = None
    if eto_openet:
        et_real = _intentar({}, "x", F.openet_mensual, lat, lon, "et")
        pr_gridmet = _intentar({}, "x", F.openet_mensual, lat, lon, "pr")
    et0_power = et0_penman_monteith_mensual(pw, lat, pw["elev"])
    # WHY prefer OpenET's ETo: gridMET is built from weather STATIONS at 4 km;
    # POWER is a 50 km model. Same FAO-56 formula, better inputs.
    et0 = eto_openet or et0_power

    # --- 3) rain: satellite (IMERG) + gridded stations (gridMET) + gauges (ACIS) + POWER
    imerg = _intentar(fu, "imerg", F.imerg_normales, lat, lon)
    acis = _intentar(fu, "acis", F.acis_lluvia, lat, lon)
    lluvia, pesos_acum = [], {}
    n_est = len(acis["normales"]) if acis else 0
    d_est = None
    for m in range(12):
        est = []
        if imerg:
            v = imerg["lluvia"][m]; est.append(("IMERG", v, max(imerg["sigma"][m], 0.15 * v, 1.0)))
        if pr_gridmet:
            v = pr_gridmet[m]; est.append(("gridMET", v, max(0.10 * v, 1.0)))
        if acis and acis["normales"]:
            v, d_est = idw([(s["normal"][m], s["d"]) for s in acis["normales"]])
            est.append((f"{L_EST}", v, max(sigma_estaciones(d_est) * v, 1.0)))
        v = pw["lluvia"][m]; est.append(("POWER", v, max(0.25 * v, 1.0)))
        valor, pesos, _ = fusion_inversa_varianza(est)
        lluvia.append(round(valor, 1))
        for k, w in pesos.items():
            pesos_acum[k] = pesos_acum.get(k, 0) + w / 12

    # --- 4) this year's rain anomaly (last 90 days) ---------------------------
    fin = dt.date.today() - dt.timedelta(days=5)
    ini = fin - dt.timedelta(days=90)
    normal90 = _normal_ventana(lluvia, ini, fin)
    anoms = []
    if diario and diario.get("lluvia90") is not None and diario.get("lluvia90_normal"):
        anoms.append(("POWER", (diario["lluvia90"] - diario["lluvia90_normal"]) / diario["lluvia90_normal"] * 100, 30))
    rec_imerg = _intentar({}, "x", F.imerg_reciente, lat, lon) if imerg else None
    if rec_imerg is not None and normal90 > 0:
        anoms.append(("IMERG", (rec_imerg - normal90) / normal90 * 100, 20))
    if acis and acis["recientes"] and normal90 > 0:
        v, d = idw([(s["total"], s["d"]) for s in acis["recientes"]])
        anoms.append((L_EST, (v - normal90) / normal90 * 100, sigma_estaciones(d) * 100))
    lluvia_anom, _, _ = fusion_inversa_varianza(anoms)

    # --- 5) soil moisture anomaly: SMAP (satellite) + SCAN (ground probes) ----
    smap = _intentar(fu, "smap", F.smap_anomalia, lat, lon)
    scan = _intentar(fu, "scan", F.scan_humedad, lat, lon)
    est_h = []
    if smap:
        est_h.append(("SMAP", smap["anom_pct"], max(smap["sigma_pct"], 10)))
    if scan:
        v, d = idw([(s["anom_pct"], s["d"]) for s in scan])
        est_h.append(("SCAN", v, 10 + 0.1 * d))           # a probe 100 km away is a weaker witness of your field
    humedad_anom, pesos_h, _ = fusion_inversa_varianza(est_h)

    # --- 6) field scale: Sentinel-2 NDVI + SRTM slope -------------------------
    prefijo = f"{lat:.3f}_{lon:.3f}".replace("-", "m").replace(".", "p")
    cap = _intentar(fu, "sentinel", F.capas_sentinel_srtm, lat, lon, carpeta_capas, prefijo)
    fu["srtm"] = fu["sentinel"]
    ndvi_mes = _intentar({}, "x", F.ndvi_mensual, lat, lon) if fu.get("sentinel") != "sin_clave" else None   # same Earth Engine login
    lst = _intentar(fu, "ecostress", F.ecostress_lst, lat, lon)

    # --- 7) soil, crop history, yields, prices --------------------------------
    sue = _intentar(fu, "ssurgo", F.ssurgo, lat, lon) or {"textura": None, "drenaje": None, "mo": None, "grupoHidro": None, "pendiente": None, "ph": None}
    sg = _intentar(fu, "soilgrids", F.soilgrids, lat, lon)
    if sg:   # nitrogen and organic carbon only exist in SoilGrids; pH: SSURGO first (finer map), SoilGrids as backup
        sue["n_gkg"], sue["soc_gkg"] = sg["n_gkg"], sg["soc_gkg"]
        sue["phFuente"] = "SSURGO" if sue.get("ph") is not None else "SoilGrids"
        if sue.get("ph") is None:
            sue["ph"] = sg["ph"]
    elif sue.get("ph") is not None:
        sue["phFuente"] = "SSURGO"
    pendiente = (cap or {}).get("pendiente") or sue.get("pendiente")     # SRTM around the field > SSURGO map-unit value
    sue["pendiente"] = round(pendiente, 1) if pendiente is not None else None   # unknown -> the page asks about erosion
    cdl = _intentar(fu, "cdl", F.cdl_historial, lat, lon) or []

    rend, precios, hist = {}, None, None
    if info:
        rend = _intentar(fu, "nass", F.nass_rendimientos, info["state"], info["county"]) or {}
        ph = _intentar(fu, "nass", F.nass_precios, info["state"])
        if ph:
            precios, hist = ph
    else:
        fu["nass"] = "error"
    ams = _intentar(fu, "ams", F.ams_precios)
    if ams:
        precios = {**(precios or {}), **ams}                 # AMS cash bids (daily) override NASS monthly

    riego = detectar_riego(et_real, pr_gridmet, lluvia, et0, ndvi_mes)

    clima = {
        "tmed": [r1(x) for x in pw["tmed"]], "tmin": [r1(x) for x in pw["tmin"]], "tmax": [r1(x) for x in pw["tmax"]],
        "lluvia": lluvia, "et0": [r1(x) for x in et0],
        "diasHelada": diario["diasHelada"] if diario else _helada_aprox(pw["tmin"]),
    }
    return {
        "id": prefijo,
        "nombre": f"{info['county_nombre']}, {info['state']}" if info else f"{lat:.3f}, {lon:.3f}",
        "lat": lat, "lon": lon, "demo": False,
        "clima": clima,
        "etReal": et_real,
        "ndviMes": ndvi_mes,
        "riego": riego,
        "lst": lst,
        "anomalia": {"lluvia": round(lluvia_anom or 0), "humedadSMAP": round(humedad_anom or 0),
                     "temp": (diario or {}).get("temp_anom", 0.0), "fecha": (diario or {}).get("fecha", fin.strftime("%Y-%m"))},
        "suelo": sue,
        "ndvi": (cap or {}).get("ndvi_medio"),
        "cdl": [c for c in cdl if c.get("cultivo")] or cdl,
        "rendimiento": rend,
        "precios": precios, "preciosHist": hist,
        "capas": (cap or {}).get("capas") or {"ndvi": None, "pendiente": None, "bounds": None},
        "fuentes": fu,
        "fusion": {"lluvia": {"pesos": pesos_acum, "nEstaciones": n_est, "distKm": r1(d_est)},
                   "humedad": {"pesos": pesos_h}},
        "generado": dt.datetime.now().isoformat(timespec="seconds"),
    }


L_EST = "stations"

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    lat, lon = float(sys.argv[1]), float(sys.argv[2])
    z = construir_zona(lat, lon)
    print(json.dumps(z, indent=1, ensure_ascii=False))
    print("\nSOURCES:", z["fuentes"], file=sys.stderr)
