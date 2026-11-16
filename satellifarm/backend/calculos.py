"""
calculos.py — Pure functions (no internet). Everything here can be tested offline.

  * et0_penman_monteith_mensual : FAO-56 reference evapotranspiration from NASA POWER
  * clasificar_textura          : USDA texture triangle (sand/silt/clay % -> class)
  * idw                         : inverse-distance weighting for ground stations
  * fusion_inversa_varianza     : blend satellite + ground estimates
  * unit conversions            : NASS (bu/acre, $/bu, ...) -> metric (t/ha, $/t)
"""
import math
import calendar

DIAS_MES = [31, 28.25, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]   # 28.25 = average February


# ---------------------------------------------------------------------------
# 1) ET0 — FAO-56 Penman-Monteith (Allen et al., 1998), monthly version
# ---------------------------------------------------------------------------
def _e0(t):
    """Saturation vapor pressure (kPa) at temperature t (°C). FAO-56 eq. 11."""
    return 0.6108 * math.exp(17.27 * t / (t + 237.3))


def _ra_mj(lat_deg, dia_juliano):
    """Extraterrestrial radiation Ra (MJ/m²/day) — FAO-56 eq. 21.
    Depends only on latitude and day of year (sun geometry), so it is exact."""
    phi = math.radians(lat_deg)
    dr = 1 + 0.033 * math.cos(2 * math.pi * dia_juliano / 365)          # inverse relative Earth–Sun distance
    delta = 0.409 * math.sin(2 * math.pi * dia_juliano / 365 - 1.39)    # solar declination
    x = max(-1.0, min(1.0, -math.tan(phi) * math.tan(delta)))
    ws = math.acos(x)                                                   # sunset hour angle
    return 24 * 60 / math.pi * 0.0820 * dr * (ws * math.sin(phi) * math.sin(delta) + math.cos(phi) * math.cos(delta) * math.sin(ws))


def et0_penman_monteith_dia(tmax, tmin, rh, u2, rs, lat, elev, dia_juliano):
    """ET0 in mm/day.
    tmax, tmin : °C (POWER T2M_MAX, T2M_MIN)
    rh         : mean relative humidity % (POWER RH2M)
    u2         : wind speed at 2 m, m/s (POWER WS2M)
    rs         : solar radiation, MJ/m²/day (POWER ALLSKY_SFC_SW_DWN, AG community)
    lat, elev  : degrees, meters
    """
    t = (tmax + tmin) / 2
    p = 101.3 * ((293 - 0.0065 * elev) / 293) ** 5.26        # atmospheric pressure (kPa) eq. 7
    gamma = 0.000665 * p                                      # psychrometric constant eq. 8
    delta = 4098 * _e0(t) / (t + 237.3) ** 2                  # slope of the vapor pressure curve eq. 13
    es = (_e0(tmax) + _e0(tmin)) / 2                          # saturation vapor pressure
    ea = es * rh / 100                                        # actual vapor pressure (from mean RH, eq. 19)
    ra = _ra_mj(lat, dia_juliano)
    rso = (0.75 + 2e-5 * elev) * ra                           # clear-sky radiation eq. 37
    rns = (1 - 0.23) * rs                                     # net shortwave (albedo 0.23 of grass) eq. 38
    sigma = 4.903e-9
    rnl = sigma * ((tmax + 273.16) ** 4 + (tmin + 273.16) ** 4) / 2 * (0.34 - 0.14 * math.sqrt(max(ea, 0))) \
        * (1.35 * min(rs / rso, 1.0) - 0.35)                 # net longwave eq. 39
    rn = rns - rnl
    g = 0.0                                                   # soil heat flux ≈ 0 for monthly averages
    num = 0.408 * delta * (rn - g) + gamma * 900 / (t + 273) * u2 * (es - ea)
    den = delta + gamma * (1 + 0.34 * u2)
    return max(0.0, num / den)


def et0_penman_monteith_mensual(clim, lat, elev):
    """clim = dict with 12-value lists: tmax, tmin, rh, u2, rs. Returns 12 values in mm/MONTH."""
    out = []
    for m in range(12):
        dia = int(sum(DIAS_MES[:m]) + 15)                     # middle of the month
        d = et0_penman_monteith_dia(clim["tmax"][m], clim["tmin"][m], clim["rh"][m], clim["u2"][m], clim["rs"][m], lat, elev, dia)
        out.append(round(d * DIAS_MES[m], 1))
    return out


# ---------------------------------------------------------------------------
# 2) USDA texture triangle
# ---------------------------------------------------------------------------
def clasificar_textura(sand, silt, clay):
    """Returns the USDA texture class (english, lowercase) — the frontend translates it."""
    if sand is None or silt is None or clay is None:
        return None
    if silt + 1.5 * clay < 15:
        return "sand"
    if silt + 2 * clay < 30:
        return "loamy sand"
    if (7 <= clay < 20 and sand > 52) or (clay < 7 and silt < 50):
        return "sandy loam"
    if 7 <= clay < 27 and 28 <= silt < 50 and sand <= 52:
        return "loam"
    if (silt >= 50 and 12 <= clay < 27) or (50 <= silt < 80 and clay < 12):
        return "silt loam"
    if silt >= 80 and clay < 12:
        return "silt"
    if 20 <= clay < 35 and silt < 28 and sand > 45:
        return "sandy clay loam"
    if 27 <= clay < 40 and 20 < sand <= 45:
        return "clay loam"
    if 27 <= clay < 40 and sand <= 20:
        return "silty clay loam"
    if clay >= 35 and sand > 45:
        return "sandy clay"
    if clay >= 40 and silt >= 40:
        return "silty clay"
    return "clay"


# ---------------------------------------------------------------------------
# 3) Spatial interpolation and data fusion
# ---------------------------------------------------------------------------
def dist_km(lat1, lon1, lat2, lon2):
    r = math.pi / 180
    h = math.sin((lat2 - lat1) * r / 2) ** 2 + math.cos(lat1 * r) * math.cos(lat2 * r) * math.sin((lon2 - lon1) * r / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h))


def idw(valores_dist, potencia=2):
    """Inverse distance weighting. valores_dist = [(value, distance_km), ...]
    WHY: a station 5 km away describes your field better than one 80 km away,
    so each station weighs 1/d² (closer = much more weight).
    Returns (estimate, effective_distance_km) or (None, None)."""
    vd = [(v, max(d, 0.5)) for v, d in valores_dist if v is not None]
    if not vd:
        return None, None
    w = [1 / d ** potencia for _, d in vd]
    est = sum(wi * v for wi, (v, _) in zip(w, vd)) / sum(w)
    d_ef = sum(wi * d for wi, (_, d) in zip(w, vd)) / sum(w)
    return est, d_ef


def fusion_inversa_varianza(estimaciones):
    """estimaciones = [(name, value, sigma), ...]  (sigma = typical error, same units as value)
    WHY inverse variance: if each source has independent error σi, the weighted mean with
    wi = (1/σi²)/Σ(1/σj²) is the combination with the SMALLEST possible error.
    A precise source (small σ) dominates; a noisy one still contributes a little.
    Returns (fused_value, {name: weight}, fused_sigma)."""
    e = [(n, v, s) for n, v, s in estimaciones if v is not None and s and s > 0]
    if not e:
        return None, {}, None
    inv = [1 / s ** 2 for _, _, s in e]
    tot = sum(inv)
    valor = sum(i * v for i, (_, v, _) in zip(inv, e)) / tot
    pesos = {n: round(i / tot, 3) for i, (n, _, _) in zip(inv, e)}
    return valor, pesos, math.sqrt(1 / tot)


def sigma_estaciones(dist_efectiva_km, base=0.05, por_km=0.003):
    """Relative error of a rain gauge estimate: ~5% at the gauge, growing ~0.3%/km with distance
    (rain changes a lot over short distances, especially summer storms). Heuristic — documented."""
    return base + por_km * (dist_efectiva_km or 0)


# ---------------------------------------------------------------------------
# 4) Units (NASS uses US units; the contract is metric)
# ---------------------------------------------------------------------------
LB_KG = 0.45359237
ACRE_HA = 0.40468564              # 1 acre = 0.4047 ha  (1 ha = 2.471 acres)
TON_CORTA_T = 0.90718474           # US short ton -> metric tonne
LB_POR_BU = {"maiz": 56, "sorgo": 56, "trigo": 60, "soya": 60, "avena": 32}


def rendimiento_a_t_ha(valor, unidad, cultivo):
    """NASS yield -> t/ha. unidad: 'BU / ACRE', 'LB / ACRE', 'TONS / ACRE'."""
    u = unidad.upper().replace(" ", "")
    if u == "BU/ACRE":
        kg = valor * LB_POR_BU[cultivo] * LB_KG
    elif u == "LB/ACRE":
        kg = valor * LB_KG
    elif u in ("TONS/ACRE", "TON/ACRE"):
        kg = valor * TON_CORTA_T * 1000
    else:
        return None
    return kg / 1000 / ACRE_HA


def precio_a_usd_t(valor, unidad, cultivo):
    """NASS price -> USD per metric tonne. unidad: '$ / BU', '$ / LB', '$ / CWT', '$ / TON'."""
    u = unidad.upper().replace(" ", "")
    if u == "$/BU":
        return valor / (LB_POR_BU[cultivo] * LB_KG / 1000)
    if u == "$/LB":
        return valor / (LB_KG / 1000)
    if u == "$/CWT":                                       # hundredweight = 100 lb
        return valor / (100 * LB_KG / 1000)
    if u in ("$/TON", "$/TONS"):
        return valor / TON_CORTA_T
    return None


def dias_del_mes(anio, mes):
    return calendar.monthrange(anio, mes)[1]
