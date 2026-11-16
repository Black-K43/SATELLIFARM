"""
fuentes.py — One function per data source. Each one returns plain Python data
(dicts/lists, metric units) or raises an exception; zona.py decides what to do
when a source fails (use a backup and mark it in "fuentes").

Keys / accounts needed (put them in backend/.env — see .env.example):
  OPENET_API_KEY  -> https://etdata.org  (free account, "API key" in your profile)
  EE_PROJECT      -> Google Earth Engine cloud project (free for non-commercial use)
  NASS_API_KEY    -> https://quickstats.nass.usda.gov/api  (free, by email)
  AMS_API_KEY     -> https://mymarketnews.ams.usda.gov  (free account, optional)
No key needed: NASA POWER, SSURGO (Soil Data Access), CDL (CropScape), ACIS, SCAN (AWDB), FCC.
"""
import os
import re
import datetime as dt
from collections import defaultdict

import logging
import requests
log = logging.getLogger("fuentes")

from calculos import (DIAS_MES, dist_km, clasificar_textura, rendimiento_a_t_ha, precio_a_usd_t)

TIMEOUT = 60
S = requests.Session()
S.headers["User-Agent"] = "SatelliFarm/0.3 (NASA Space Apps 2026 hackathon)"
MESES_POWER = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
HOY = dt.date.today


# =============================================================================
# 0) County / state of the point (to query NASS and to name the zone)
# =============================================================================
def condado(lat, lon):
    """FCC Area API (free, no key): point -> county + state."""
    r = S.get("https://geo.fcc.gov/api/census/area", params={"lat": lat, "lon": lon, "format": "json"}, timeout=TIMEOUT)
    r.raise_for_status()
    res = r.json()["results"][0]
    nombre = res["county_name"]                               # e.g. "Story County"
    corto = re.sub(r"\s+(County|Parish|Borough|Census Area)$", "", nombre)
    return {"county": corto.upper(), "county_nombre": nombre, "state": res["state_code"], "fips": res["county_fips"]}


# =============================================================================
# 1) NASA POWER — temperature, humidity, wind, radiation, rain (backbone, no key)
# =============================================================================
def power_climatologia(lat, lon):
    """Monthly normals. Returns dict of 12-value lists + elevation.
    WHY POWER: global, free, no key, and has every variable FAO-56 needs for ET0."""
    params = {"parameters": "T2M,T2M_MAX,T2M_MIN,PRECTOTCORR,ALLSKY_SFC_SW_DWN,RH2M,WS2M",
              "community": "AG", "longitude": lon, "latitude": lat, "format": "JSON"}
    r = S.get("https://power.larc.nasa.gov/api/temporal/climatology/point", params=params, timeout=TIMEOUT)
    r.raise_for_status()
    j = r.json()
    p = j["properties"]["parameter"]
    m = lambda k: [p[k][mes] for mes in MESES_POWER]
    return {
        "tmed": m("T2M"), "tmax": m("T2M_MAX"), "tmin": m("T2M_MIN"),
        "lluvia": [v * DIAS_MES[i] for i, v in enumerate(m("PRECTOTCORR"))],   # mm/day -> mm/month
        "rs": m("ALLSKY_SFC_SW_DWN"), "rh": m("RH2M"), "u2": m("WS2M"),
        "elev": j["geometry"]["coordinates"][2],
    }


def power_diario(lat, lon, inicio, fin, parametros="T2M,T2M_MIN,PRECTOTCORR"):
    """Daily series {param: {date: value}} (missing = -999 -> None)."""
    params = {"parameters": parametros, "community": "AG", "longitude": lon, "latitude": lat,
              "start": inicio.strftime("%Y%m%d"), "end": fin.strftime("%Y%m%d"), "format": "JSON"}
    r = S.get("https://power.larc.nasa.gov/api/temporal/daily/point", params=params, timeout=TIMEOUT * 3)
    r.raise_for_status()
    p = r.json()["properties"]["parameter"]
    return {k: {dt.datetime.strptime(d, "%Y%m%d").date(): (None if v is None or v <= -998 else v) for d, v in serie.items()}
            for k, serie in p.items()}


def dias_helada_y_reciente(lat, lon, anio0=2001):
    """From POWER daily data:
       - average number of frost days (Tmin < 0 °C) per month
       - temperature and rain of the last 90 days vs the same window in past years (anomaly)."""
    fin = HOY() - dt.timedelta(days=5)                        # POWER has a few days of latency
    d = power_diario(lat, lon, dt.date(anio0, 1, 1), fin)
    tmin, t2m, pr = d["T2M_MIN"], d["T2M"], d["PRECTOTCORR"]
    cuenta = defaultdict(int)
    anios = set()
    for f, v in tmin.items():
        if v is not None and f.year < fin.year:
            anios.add(f.year)
            if v < 0:
                cuenta[f.month] += 1
    n = max(len(anios), 1)
    helada = [round(cuenta[m] / n, 1) for m in range(1, 13)]

    ini90 = fin - dt.timedelta(days=90)
    def ventana(serie, anio_offset):
        a, b = ini90.replace(year=ini90.year - anio_offset), fin.replace(year=fin.year - anio_offset)
        vals = [v for f, v in serie.items() if a < f <= b and v is not None]
        return vals
    t_rec = ventana(t2m, 0); p_rec = ventana(pr, 0)
    t_hist, p_hist = [], []
    for k in range(1, min(20, fin.year - anio0)):
        tv, pv = ventana(t2m, k), ventana(pr, k)
        if len(tv) > 80: t_hist.append(sum(tv) / len(tv))
        if len(pv) > 80: p_hist.append(sum(pv))
    return {
        "diasHelada": helada,
        "temp_anom": round(sum(t_rec) / len(t_rec) - sum(t_hist) / len(t_hist), 1) if t_rec and t_hist else 0.0,
        "lluvia90": sum(p_rec) if p_rec else None,
        "lluvia90_normal": sum(p_hist) / len(p_hist) if p_hist else None,
        "fecha": fin.strftime("%Y-%m"),
    }


# =============================================================================
# 2) OpenET — water: reference ET (ETo), ACTUAL ET of the field, and rain (gridMET)
# =============================================================================
def openet_mensual(lat, lon, variable, inicio="2021-01-01", fin=None):
    """POST /raster/timeseries/point. variable: 'eto' (reference ET, gridMET),
    'et' (actual ET, ensemble of 6 satellite models, 30 m), 'pr' (gridMET precipitation).
    Returns 12-value list = average per calendar month (mm/month)."""
    key = os.getenv("OPENET_API_KEY")
    if not key:
        raise RuntimeError("sin_clave")
    fin = fin or (HOY().replace(day=1) - dt.timedelta(days=1)).isoformat()
    nombre_api = {"eto": "ETo", "et": "ET", "pr": "PR"}.get(variable, variable)
    body = {"date_range": [inicio, fin], "interval": "monthly", "geometry": [lon, lat],
            "model": "Ensemble", "variable": nombre_api, "reference_et": "gridMET",
            "units": "mm", "file_format": "JSON"}
    r = S.post("https://openet-api.org/raster/timeseries/point", json=body,
               headers={"Authorization": key, "accept": "application/json"}, timeout=TIMEOUT * 2)
    r.raise_for_status()
    filas = r.json()
    por_mes = defaultdict(list)
    for fila in filas:
        k_t = next(k for k in fila if k.lower() in ("time", "date"))
        k_v = next(k for k in fila if k != k_t)
        if fila[k_v] is not None:
            por_mes[int(str(fila[k_t])[5:7])].append(float(fila[k_v]))
    if len(por_mes) < 12:
        raise RuntimeError("datos_incompletos")
    return [round(sum(por_mes[m]) / len(por_mes[m]), 1) for m in range(1, 13)]


# =============================================================================
# 3) Google Earth Engine — GPM-IMERG, SMAP, Sentinel-2, SRTM (one login for all)
# =============================================================================
_EE_OK = None
def _ee():
    """Initialize Earth Engine once. Run `earthengine authenticate` one time on your PC first."""
    global _EE_OK
    import ee
    if _EE_OK is None:
        proj = os.getenv("EE_PROJECT")
        if not proj:
            raise RuntimeError("sin_clave")
        ee.Initialize(project=proj)
        _EE_OK = True
    return ee


def imerg_normales(lat, lon, anio0=2001, anio1=2024):
    """GPM-IMERG Final monthly (0.1° ≈ 11 km). Band 'precipitation' is a RATE (mm/hr),
    so monthly total = rate × 24 × days. 'randomError' gives the uncertainty -> used in fusion."""
    ee = _ee()
    col = (ee.ImageCollection("NASA/GPM_L3/IMERG_MONTHLY_V07")
           .filterDate(f"{anio0}-01-01", f"{anio1 + 1}-01-01").select(["precipitation", "randomError"]))
    filas = col.getRegion(ee.Geometry.Point(lon, lat), 11132).getInfo()
    cab, datos = filas[0], filas[1:]
    i_t, i_p, i_e = cab.index("time"), cab.index("precipitation"), cab.index("randomError")
    p, e = defaultdict(list), defaultdict(list)
    for f in datos:
        fecha = dt.datetime.utcfromtimestamp(f[i_t] / 1000)
        horas = 24 * DIAS_MES[fecha.month - 1]
        if f[i_p] is not None:
            p[fecha.month].append(f[i_p] * horas)
            e[fecha.month].append((f[i_e] or 0) * horas)
    normal = [sum(p[m]) / len(p[m]) for m in range(1, 13)]
    err = [sum(e[m]) / len(e[m]) for m in range(1, 13)]
    return {"lluvia": normal, "sigma": err}


def imerg_reciente(lat, lon, dias=90):
    """Rain of the last 90 days from IMERG half-hourly (sum × 0.5 h)."""
    ee = _ee()
    fin = ee.Date(dt.datetime.utcnow().isoformat()).advance(-3, "day")       # IMERG Early/Late latency
    ini = fin.advance(-dias, "day")
    img = ee.ImageCollection("NASA/GPM_L3/IMERG_V07").filterDate(ini, fin).select("precipitation").sum().multiply(0.5)
    v = img.reduceRegion(ee.Reducer.mean(), ee.Geometry.Point(lon, lat), 11132).get("precipitation").getInfo()
    return v


def smap_anomalia(lat, lon, dias=30):
    """SMAP L4 surface soil moisture (sm_surface, m³/m³, 9 km): mean of the last 30 days
    vs the SAME 30-day window in every year since 2016.
    WHY an anomaly and not the raw value: a 9 km pixel and a point sensor never agree in
    absolute terms, but both agree on 'wetter or drier than usual' — anomalies can be compared and fused."""
    ee = _ee()
    col = ee.ImageCollection("NASA/SMAP/SPL4SMGP/008").select("sm_surface")
    pt = ee.Geometry.Point(lon, lat)
    hoy = dt.datetime.utcnow()
    fin = hoy - dt.timedelta(days=4)
    ini = fin - dt.timedelta(days=dias)
    def media(a, b):
        return col.filterDate(a.isoformat(), b.isoformat()).mean().reduceRegion(ee.Reducer.mean(), pt, 11000).get("sm_surface")
    reciente = media(ini, fin).getInfo()
    hist = []
    for k in range(1, fin.year - 2015):
        try:
            v = media(ini.replace(year=ini.year - k), fin.replace(year=fin.year - k)).getInfo()
            if v is not None: hist.append(v)
        except Exception:
            pass
    if reciente is None or not hist:
        raise RuntimeError("sin_datos")
    normal = sum(hist) / len(hist)
    return {"anom_pct": (reciente - normal) / normal * 100, "actual": reciente, "normal": normal,
            "sigma_pct": 0.04 / normal * 100}               # SMAP L4 target error ≈ 0.04 m³/m³


def capas_sentinel_srtm(lat, lon, carpeta, prefijo, radio_m=1500):
    """Field-scale color layers (PNG) + numbers:
       - NDVI median of last summer, Sentinel-2 10 m (vegetation vigor; low NDVI = bare or stressed)
       - slope from SRTM 30 m (erosion risk)."""
    ee = _ee()
    pt = ee.Geometry.Point(lon, lat)
    region = pt.buffer(radio_m).bounds()
    anio = HOY().year if HOY().month > 9 else HOY().year - 1
    s2 = (ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED").filterBounds(region)
          .filterDate(f"{anio}-06-01", f"{anio}-09-01").filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 20)).median())
    ndvi = s2.normalizedDifference(["B8", "B4"]).rename("ndvi")
    pend_grados = ee.Terrain.slope(ee.Image("USGS/SRTMGL1_003"))
    pend_pct = pend_grados.multiply(3.14159265 / 180).tan().multiply(100).rename("pend")

    stats = ndvi.addBands(pend_pct).reduceRegion(ee.Reducer.mean(), pt.buffer(150), 10).getInfo()
    coords = region.coordinates().getInfo()[0]
    lats, lons = [c[1] for c in coords], [c[0] for c in coords]
    bounds = [[min(lats), min(lons)], [max(lats), max(lons)]]
    os.makedirs(carpeta, exist_ok=True)
    rutas = {}
    for nombre, img, vis in [("ndvi", ndvi, {"min": 0, "max": 0.9, "palette": ["a0522d", "f5deb3", "9acd32", "006400"]}),
                             ("pendiente", pend_pct, {"min": 0, "max": 12, "palette": ["1a9850", "fee08b", "d73027"]})]:
        url = img.getThumbURL({**vis, "region": region, "dimensions": 512, "format": "png"})
        png = S.get(url, timeout=TIMEOUT).content
        archivo = f"{prefijo}_{nombre}.png"
        with open(os.path.join(carpeta, archivo), "wb") as f:
            f.write(png)
        rutas[nombre] = f"/capas/{archivo}"
    return {"ndvi_medio": stats.get("ndvi"), "pendiente": stats.get("pend"), "capas": {**rutas, "bounds": bounds}}


def ndvi_mensual(lat, lon, anios=3):
    """Typical NDVI of the FIELD for each calendar month (Sentinel-2, 10 m), last 3 years.
    Clouds and shadows are removed with the Scene Classification band (SCL) before the median.
    WHY: the NDVI curve shows when the field is green (crop growing) and when it is BARE
    (NDVI < 0.3) -> erosion risk and the window for a cover crop."""
    ee = _ee()
    pt = ee.Geometry.Point(lon, lat)
    fin = HOY().replace(day=1)
    ini = fin.replace(year=fin.year - anios)
    def preparar(img):
        scl = img.select("SCL")
        ok = scl.neq(3).And(scl.neq(8)).And(scl.neq(9)).And(scl.neq(10)).And(scl.neq(11))   # shadow, clouds, cirrus, snow
        return img.normalizedDifference(["B8", "B4"]).rename("ndvi").updateMask(ok)
    col = (ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED").filterBounds(pt)
           .filterDate(ini.isoformat(), fin.isoformat()).filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 60)).map(preparar))
    def por_mes(m):
        img = col.filter(ee.Filter.calendarRange(m, m, "month")).median()
        return img.reduceRegion(ee.Reducer.mean(), pt.buffer(30), 10).get("ndvi")
    vals = ee.List.sequence(1, 12).map(lambda m: por_mes(ee.Number(m))).getInfo()
    vals = [round(v, 2) if v is not None else None for v in vals]
    if sum(v is not None for v in vals) < 8:
        raise RuntimeError("sin_datos")
    return vals


# =============================================================================
# 3b) ECOSTRESS — land SURFACE temperature at 70 m (NASA Earthdata, via earthaccess)
# =============================================================================
def ecostress_lst(lat, lon, max_pasadas=20):
    """Summer (Jun–Aug) land surface temperature of the field from ECOSTRESS L2T LSTE (tiled, 70 m).
    Needs a free NASA Earthdata login in .env (EARTHDATA_USERNAME / EARTHDATA_PASSWORD).
    WHY: weather data give AIR temperature at 2 m; ECOSTRESS gives the temperature of the soil and
    leaves. A surface much hotter than the air = plants not transpiring enough (heat/water stress).
    Only daytime passes (10:00–16:00 local) are used, and values outside 0–70 °C are discarded (clouds)."""
    if not (os.getenv("EARTHDATA_USERNAME") and os.getenv("EARTHDATA_PASSWORD")):
        raise RuntimeError("sin_clave")
    import earthaccess, rasterio
    from rasterio.warp import transform
    earthaccess.login(strategy="environment")
    anio = HOY().year if HOY().month > 9 else HOY().year - 1
    gran = earthaccess.search_data(short_name="ECO_L2T_LSTE", bounding_box=(lon - 0.01, lat - 0.01, lon + 0.01, lat + 0.01),
                                   temporal=(f"{anio}-06-01", f"{anio}-08-31"), count=80)
    fs = earthaccess.get_fsspec_https_session()
    valores = []
    for g in gran:
        if len(valores) >= max_pasadas:
            break
        try:
            inicio = g["umm"]["TemporalExtent"]["RangeDateTime"]["BeginningDateTime"]       # UTC
            hora_local = (int(inicio[11:13]) + round(lon / 15)) % 24                       # approx. solar time
            if not 10 <= hora_local <= 16:
                continue
            url = next(u for u in g.data_links() if u.endswith("_LST.tif"))
            with fs.open(url) as f, rasterio.open(f) as ds:
                xs, ys = transform("EPSG:4326", ds.crs, [lon], [lat])
                v = next(ds.sample([(xs[0], ys[0])]))[0]
            if v is None or v != v:          # NaN
                continue
            k = v * 0.02 if v > 1000 else v  # older products store LST scaled by 0.02
            c = k - 273.15
            if 0 < c < 70:
                valores.append(c)
        except Exception:
            continue
    if len(valores) < 3:
        raise RuntimeError("sin_datos")
    valores.sort()
    return {"verano": round(sum(valores) / len(valores), 1), "max": round(valores[int(0.9 * (len(valores) - 1))], 1), "n": len(valores)}


# =============================================================================
# 4) USDA SSURGO — soil (Soil Data Access, SQL over REST, no key)
# =============================================================================
def ssurgo(lat, lon):
    """Dominant soil component at the point + its top horizon."""
    sql = f"""
    SELECT TOP 1 mu.muname, c.compname, c.comppct_r, c.hydgrp, c.drainagecl, c.slope_r,
           ch.om_r, ch.sandtotal_r, ch.silttotal_r, ch.claytotal_r, ch.awc_r, ch.kwfact, ch.ph1to1h2o_r
    FROM mapunit mu
    JOIN component c ON c.mukey = mu.mukey
    JOIN chorizon ch ON ch.cokey = c.cokey
    WHERE mu.mukey IN (SELECT * FROM SDA_Get_Mukey_from_intersection_with_WktWgs84('point({lon} {lat})'))
      AND c.majcompflag = 'Yes'
    ORDER BY c.comppct_r DESC, ch.hzdept_r ASC"""
    r = S.post("https://sdmdataaccess.sc.egov.usda.gov/tabular/post.rest",
               json={"query": sql, "format": "JSON+COLUMNNAME"}, timeout=TIMEOUT)
    r.raise_for_status()
    tabla = r.json().get("Table")
    if not tabla or len(tabla) < 2:
        raise RuntimeError("sin_datos")
    fila = dict(zip(tabla[0], tabla[1]))
    num = lambda k: float(fila[k]) if fila.get(k) not in (None, "") else None
    return {
        "nombreUnidad": fila["muname"],
        "textura": clasificar_textura(num("sandtotal_r"), num("silttotal_r"), num("claytotal_r")),
        "drenaje": fila.get("drainagecl") or "Unknown",
        "mo": round(num("om_r"), 1) if num("om_r") is not None else None,
        "grupoHidro": fila.get("hydgrp") or "?",
        "pendiente": num("slope_r"),
        "aguaDisponible": num("awc_r"),      # cm water / cm soil
        "kw": num("kwfact"),                  # soil erodibility factor
        "ph": num("ph1to1h2o_r"),             # pH in water 1:1 (top horizon)
        "fuente": "SSURGO",
    }


# =============================================================================
# 4b) ISRIC SoilGrids 250 m — total nitrogen, organic carbon and pH (0-5 cm), no key
#     SoilGrids stores integers: nitrogen cg/kg (/100 -> g/kg), soc dg/kg (/10 -> g/kg),
#     phh2o pH*10 (/10). The public API allows ~5 requests/min: the zone cache helps.
# =============================================================================
def soilgrids(lat, lon):
    """REST API first (fast, but ISRIC has paused it at times); if it fails, the official WCS map service."""
    try:
        return _soilgrids_rest(lat, lon)
    except Exception as e:
        log.warning("SoilGrids REST failed (%s); trying WCS", e)
        return _soilgrids_wcs(lat, lon)


def _soilgrids_wcs(lat, lon, d=0.003):
    """ISRIC WCS (maps.isric.org): a tiny GeoTIFF around the point, read the center pixel.
    Same integer units as the REST API. Needs rasterio (already in requirements.txt)."""
    import rasterio
    from rasterio.io import MemoryFile
    out = {}
    for prop in ("nitrogen", "soc", "phh2o"):
        params = [("map", f"/map/{prop}.map"), ("SERVICE", "WCS"), ("VERSION", "2.0.1"), ("REQUEST", "GetCoverage"),
                  ("COVERAGEID", f"{prop}_0-5cm_mean"), ("FORMAT", "image/tiff"),
                  ("SUBSET", f"long({lon - d},{lon + d})"), ("SUBSET", f"lat({lat - d},{lat + d})"),
                  ("SUBSETTINGCRS", "http://www.opengis.net/def/crs/EPSG/0/4326"),
                  ("OUTPUTCRS", "http://www.opengis.net/def/crs/EPSG/0/4326")]
        r = S.get("https://maps.isric.org/mapserv", params=params, timeout=TIMEOUT)
        r.raise_for_status()
        with MemoryFile(r.content) as mf, mf.open() as ds:
            fila, col = ds.index(lon, lat)
            fila, col = min(max(fila, 0), ds.height - 1), min(max(col, 0), ds.width - 1)
            v = float(ds.read(1)[fila, col])
            out[prop] = None if (ds.nodata is not None and v == ds.nodata) or v < 0 else v
    if all(v is None for v in out.values()):
        raise RuntimeError("sin_datos")
    div = {"nitrogen": 100, "soc": 10, "phh2o": 10}
    v = {k: (out[k] / div[k] if out[k] is not None else None) for k in div}
    return {"n_gkg": v["nitrogen"], "soc_gkg": v["soc"], "ph": v["phh2o"], "fuente": "SoilGrids 250 m WCS (0-5 cm)"}


def _soilgrids_rest(lat, lon):
    r = S.get("https://rest.isric.org/soilgrids/v2.0/properties/query",
              params=[("lon", lon), ("lat", lat), ("property", "nitrogen"), ("property", "soc"),
                      ("property", "phh2o"), ("depth", "0-5cm"), ("value", "mean")], timeout=TIMEOUT)
    r.raise_for_status()
    capas = {c["name"]: c["depths"][0]["values"].get("mean") for c in r.json()["properties"]["layers"]}
    div = {"nitrogen": 100, "soc": 10, "phh2o": 10}
    v = {k: (capas[k] / div[k] if capas.get(k) is not None else None) for k in div}
    if all(x is None for x in v.values()):
        raise RuntimeError("sin_datos")
    return {"n_gkg": v["nitrogen"], "soc_gkg": v["soc"], "ph": v["phh2o"], "fuente": "SoilGrids 250 m (0-5 cm)"}


# =============================================================================
# 5) USDA CDL (CropScape) — what was planted in the field each year (no key)
# =============================================================================
CDL_A_CULTIVO = {1: "maiz", 2: "algodon", 4: "sorgo", 5: "soya", 6: "girasol", 24: "trigo",
                 28: "avena", 36: "alfalfa", 51: "garbanzo", 54: "tomate"}

def cdl_historial(lat, lon, anios=3):
    """GetCDLValue needs coordinates in CONUS Albers (EPSG:5070), not lat/lon."""
    from pyproj import Transformer
    x, y = Transformer.from_crs("EPSG:4326", "EPSG:5070", always_xy=True).transform(lon, lat)
    out, anio = [], HOY().year - 1
    while len(out) < anios and anio > HOY().year - 6:
        try:
            r = S.get("https://nassgeodata.gmu.edu/axis2/services/CDLService/GetCDLValue",
                      params={"year": anio, "x": round(x, 2), "y": round(y, 2)}, timeout=TIMEOUT)
            m = re.search(r"value:\s*(\d+)", r.text)
            cat = re.search(r'category:\s*"([^"]*)"', r.text)
            if m:
                cod = int(m.group(1))
                out.append({"anio": anio, "codigo": cod, "categoria": cat.group(1) if cat else None,
                            "cultivo": CDL_A_CULTIVO.get(cod)})
        except Exception:
            pass
        anio -= 1
    if not out:
        raise RuntimeError("sin_datos")
    return out


# =============================================================================
# 6) USDA NASS QuickStats — county yields and prices received
# =============================================================================
NASS = {   # our id: (commodity_desc, text that must appear in short_desc)
    "maiz": ("CORN", "CORN, GRAIN"), "soya": ("SOYBEANS", "SOYBEANS"), "trigo": ("WHEAT", "WHEAT"),
    "sorgo": ("SORGHUM", "SORGHUM, GRAIN"), "algodon": ("COTTON", "COTTON, UPLAND"), "alfalfa": ("HAY", "HAY, ALFALFA"),
    "girasol": ("SUNFLOWER", "SUNFLOWER"), "avena": ("OATS", "OATS"), "garbanzo": ("CHICKPEAS", "CHICKPEAS"),
    "tomate": ("TOMATOES", "TOMATOES, PROCESSING"),
}
_EXCLUIR = ("IRRIGATED", "NON-IRRIGATED", "FOLLOWING", "CONTINUOUS", "SPRING", "DURUM", "PIMA", "ORGANIC")

def _nass(params):
    key = os.getenv("NASS_API_KEY")
    if not key:
        raise RuntimeError("sin_clave")
    r = S.get("https://quickstats.nass.usda.gov/api/api_GET/", params={"key": key, "format": "JSON", **params}, timeout=TIMEOUT)
    if r.status_code == 400:            # NASS answers 400 when there are no rows
        return []
    r.raise_for_status()
    return r.json().get("data", [])

def _valor(s):
    try:
        return float(str(s).replace(",", ""))
    except ValueError:
        return None                      # "(D)" = withheld, "(NA)" ...

def _unidad(short_desc):
    m = re.search(r"MEASURED IN (.+)$", short_desc)
    return m.group(1).strip() if m else ""

def nass_rendimientos(state, county, anios=5):
    """Average yield of the last 5 years for each crop (county; if missing, state). t/ha."""
    out = {}
    for cid, (comm, filtro) in NASS.items():
        excl = tuple(x for x in _EXCLUIR if not (cid == "trigo" and x == "SPRING"))
        for nivel in ("COUNTY", "STATE"):
            p = {"commodity_desc": comm, "statisticcat_desc": "YIELD", "agg_level_desc": nivel,
                 "state_alpha": state, "year__GE": HOY().year - anios - 1}
            if nivel == "COUNTY": p["county_name"] = county
            if cid == "trigo": p["class_desc"] = "WINTER"
            filas = [f for f in _nass(p) if f["short_desc"].startswith(filtro)
                     and not any(x in f["short_desc"] for x in excl) and f.get("reference_period_desc") == "YEAR"]
            vals = [(_valor(f["Value"]), _unidad(f["short_desc"])) for f in filas]
            vals = [(v, u) for v, u in vals if v]
            if vals:
                t_ha = [rendimiento_a_t_ha(v, u, cid) for v, u in vals]
                t_ha = [x for x in t_ha if x]
                if t_ha:
                    out[cid] = round(sum(t_ha) / len(t_ha), 2)
                    break
    return out

def nass_precios(state):
    """Latest monthly 'price received' (state, else national) + last 12 months history. USD/t."""
    precios, hist = {}, {}
    for cid, (comm, filtro) in NASS.items():
        for nivel, extra in (("STATE", {"state_alpha": state}), ("NATIONAL", {})):
            filas = _nass({"commodity_desc": comm, "statisticcat_desc": "PRICE RECEIVED", "agg_level_desc": nivel,
                           "freq_desc": "MONTHLY", "year__GE": HOY().year - 2, **extra})
            filas = [f for f in filas if f["short_desc"].startswith(filtro) and not any(x in f["short_desc"] for x in _EXCLUIR)]
            serie = []
            for f in filas:
                v = _valor(f["Value"])
                usd_t = precio_a_usd_t(v, _unidad(f["short_desc"]), cid) if v else None
                mes = MESES_POWER.index(f["reference_period_desc"][:3]) + 1 if f["reference_period_desc"][:3] in MESES_POWER else None
                if usd_t and mes:
                    serie.append((int(f["year"]), mes, round(usd_t, 1)))
            if serie:
                serie.sort()
                precios[cid] = serie[-1][2]
                hist[cid] = [{"fecha": f"{a}-{m:02d}", "usd_t": p} for a, m, p in serie[-12:]]
                break
    return precios, hist


# =============================================================================
# 7) USDA AMS MyMarketNews — daily cash bids (optional, needs report ids)
# =============================================================================
# Find report "slug ids" for your region at https://mymarketnews.ams.usda.gov (e.g. a state's daily grain bids)
AMS_REPORTES = {}   # e.g. {"maiz": 2850, "soya": 2850}  <- fill in with the report ids you want

def ams_precios():
    key = os.getenv("AMS_API_KEY")
    if not key or not AMS_REPORTES:
        raise RuntimeError("sin_clave")
    out = {}
    for cid, slug in AMS_REPORTES.items():
        r = S.get(f"https://marsapi.ams.usda.gov/services/v1.2/reports/{slug}", params={"lastDays": 7},
                  auth=(key, ""), timeout=TIMEOUT)                                  # basic auth: key as user, empty password
        r.raise_for_status()
        filas = [f for f in r.json().get("results", []) if NASS[cid][0].lower() in str(f.get("commodity", "")).lower()]
        vals = [_valor(f.get("avg_price") or f.get("price_avg") or f.get("price")) for f in filas]
        vals = [v for v in vals if v]
        if vals:
            out[cid] = precio_a_usd_t(sum(vals) / len(vals), "$ / BU", cid) if cid in ("maiz", "soya", "trigo", "sorgo", "avena") else None
    return {k: round(v, 1) for k, v in out.items() if v}


# =============================================================================
# 8) GROUND SENSORS
#    a) ACIS (NOAA Regional Climate Centers): rain gauges — COOP, GHCN and CoCoRaHS volunteers
#    b) NRCS SCAN (AWDB REST API): soil moisture probes at 2/4/8/20/40 inches
# =============================================================================
def acis_lluvia(lat, lon, radio_grados=0.5, anio0=2001):
    """Nearby gauges: monthly normals and last-90-day rain, each with its distance (km).
    Units from ACIS are inches -> converted to mm."""
    bbox = f"{lon - radio_grados},{lat - radio_grados},{lon + radio_grados},{lat + radio_grados}"
    fin = HOY() - dt.timedelta(days=2)
    def pedir(sdate, edate, elem):
        r = S.post("https://data.rcc-acis.org/MultiStnData",
                   json={"bbox": bbox, "sdate": sdate, "edate": edate, "elems": [elem], "meta": "name,ll"}, timeout=TIMEOUT * 2)
        r.raise_for_status()
        return r.json().get("data", [])
    pulg = lambda v: 0.0 if v == "T" else (None if v in ("M", "S", "") else float(str(v).rstrip("A")) * 25.4)

    # monthly sums 2001..last year
    mens = pedir(f"{anio0}-01", f"{fin.year - 1}-12", {"name": "pcpn", "interval": "mly", "duration": "mly", "reduce": "sum", "maxmissing": 3})
    estaciones = []
    for st in mens:
        lon_s, lat_s = st["meta"]["ll"]
        por_mes = defaultdict(list)
        for i, v in enumerate(st["data"]):
            val = pulg(v[0] if isinstance(v, list) else v)
            if val is not None:
                por_mes[i % 12 + 1].append(val)
        if all(len(por_mes[m]) >= 8 for m in range(1, 13)):                 # at least 8 years per month
            estaciones.append({"nombre": st["meta"]["name"], "d": dist_km(lat, lon, lat_s, lon_s),
                               "normal": [sum(por_mes[m]) / len(por_mes[m]) for m in range(1, 13)]})
    # last 90 days
    rec = pedir((fin - dt.timedelta(days=90)).isoformat(), fin.isoformat(), {"name": "pcpn"})
    recientes = []
    for st in rec:
        lon_s, lat_s = st["meta"]["ll"]
        vals = [pulg(v[0] if isinstance(v, list) else v) for v in st["data"]]
        ok = [v for v in vals if v is not None]
        if len(ok) >= 80:
            recientes.append({"nombre": st["meta"]["name"], "d": dist_km(lat, lon, lat_s, lon_s), "total": sum(ok)})
    if not estaciones and not recientes:
        raise RuntimeError("sin_estaciones")
    return {"normales": estaciones, "recientes": recientes}


AWDB = "https://wcc.sc.egov.usda.gov/awdbRestApi/services/v1"

def scan_humedad(lat, lon, radio_km=150, dias=30):
    """Soil moisture anomaly at the nearest SCAN stations (shallowest probe, usually 2 in = 5 cm).
    Swagger to check parameter names: https://wcc.sc.egov.usda.gov/awdbRestApi/swagger-ui/index.html"""
    r = S.get(f"{AWDB}/stations", params={"networkCds": "SCAN", "elements": "SMS", "activeOnly": "true"}, timeout=TIMEOUT)
    r.raise_for_status()
    cerca = sorted(({"triplet": s["stationTriplet"], "nombre": s.get("name"), "d": dist_km(lat, lon, s["latitude"], s["longitude"])}
                    for s in r.json() if s.get("latitude")), key=lambda s: s["d"])
    cerca = [s for s in cerca if s["d"] <= radio_km][:3]
    if not cerca:
        raise RuntimeError("sin_estaciones")
    fin = HOY() - dt.timedelta(days=2)
    ini = fin - dt.timedelta(days=dias)
    out = []
    for s in cerca:
        r = S.get(f"{AWDB}/data", params={"stationTriplets": s["triplet"], "elements": "SMS:*", "duration": "DAILY",
                                          "beginDate": dt.date(2016, 1, 1).isoformat(), "endDate": fin.isoformat()}, timeout=TIMEOUT * 2)
        if not r.ok:
            continue
        series = r.json()[0].get("data", [])
        if not series:
            continue
        # shallowest sensor (heightDepth closest to 0, e.g. -2 inches)
        serie = max(series, key=lambda x: x["stationElement"].get("heightDepth") or -999)
        vals = {dt.date.fromisoformat(v["date"][:10]): v.get("value") for v in serie.get("values", []) if v.get("value") is not None}
        actual = [v for f, v in vals.items() if ini < f <= fin]
        hist = []
        for k in range(1, fin.year - 2015):
            a, b = ini.replace(year=ini.year - k), fin.replace(year=fin.year - k)
            w = [v for f, v in vals.items() if a < f <= b]
            if len(w) > dias * 0.7: hist.append(sum(w) / len(w))
        if len(actual) > dias * 0.7 and len(hist) >= 3:
            normal = sum(hist) / len(hist)
            out.append({"nombre": s["nombre"], "d": s["d"], "anom_pct": (sum(actual) / len(actual) - normal) / normal * 100})
    if not out:
        raise RuntimeError("sin_datos")
    return out
