"""
servidor.py — Small web server for SatelliFarm.

  cd backend
  pip install -r requirements.txt
  python servidor.py            -> open http://localhost:8000

  GET /               -> the page (../index.html)
  GET /api/zona?lat=&lon=  -> zone JSON (cached 7 days per ~1 km cell, because the
                              first build takes 30–60 s and APIs have quotas)
  GET /capas/...      -> NDVI / slope PNG layers generated with Earth Engine
  GET /api/appeears   -> list of the AppEEARS CSVs in ../datos/appeears (the page loads them
                         by itself, so a new AppEEARS export only needs to be copied there)
"""
import os
import json
import time
import logging

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
except ImportError:
    pass

from zona import construir_zona

AQUI = os.path.dirname(os.path.abspath(__file__))
CAPAS = os.path.join(AQUI, "capas")
CACHE = os.path.join(AQUI, "cache")
INDEX = os.path.join(AQUI, "..", "index.html")
APPEEARS = os.path.join(AQUI, "..", "datos", "appeears")
os.makedirs(CAPAS, exist_ok=True)
os.makedirs(CACHE, exist_ok=True)
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="SatelliFarm API")
app.mount("/capas", StaticFiles(directory=CAPAS), name="capas")
if os.path.isdir(APPEEARS):
    app.mount("/datos/appeears", StaticFiles(directory=APPEEARS), name="appeears")


@app.get("/api/appeears")
def api_appeears():
    if not os.path.isdir(APPEEARS):
        return []
    return sorted(f for f in os.listdir(APPEEARS) if f.lower().endswith(".csv"))


@app.get("/")
def pagina():
    return FileResponse(INDEX)


@app.get("/api/zona")
def api_zona(lat: float = Query(..., ge=24.5, le=49.5), lon: float = Query(..., ge=-125, le=-66.9)):
    archivo = os.path.join(CACHE, f"{lat:.2f}_{lon:.2f}.json")          # 0.01° ≈ 1 km cell
    if os.path.exists(archivo) and time.time() - os.path.getmtime(archivo) < 7 * 86400:
        with open(archivo, encoding="utf-8") as f:
            return json.load(f)
    try:
        z = construir_zona(lat, lon, CAPAS)
    except Exception as e:
        logging.exception("zone failed")
        raise HTTPException(status_code=502, detail=f"NASA POWER unavailable: {e}")
    with open(archivo, "w", encoding="utf-8") as f:
        json.dump(z, f, ensure_ascii=False)
    return z


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
