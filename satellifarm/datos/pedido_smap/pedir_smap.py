"""
Sends the SMAP soil-moisture request to AppEEARS and downloads the CSV into datos/appeears.

  py -m pip install requests
  py pedir_smap.py           (asks for your NASA Earthdata user and password)

WHY a new request: the previous export asked for 'Soil_Moisture_Retrieval_Data_AM_albedo'
(surface albedo, ~0.07), not soil moisture. This one asks for 'AM_soil_moisture' (m³/m³)
+ 'AM_retrieval_qual_flag' (to keep only good-quality retrievals).
"""
import getpass, json, os, time, requests

API = "https://appeears.earthdatacloud.nasa.gov/api"
AQUI = os.path.dirname(os.path.abspath(__file__))
DESTINO = os.path.join(AQUI, "..", "appeears")

usuario = input("Earthdata user: ")
clave = getpass.getpass("Earthdata password: ")
token = requests.post(f"{API}/login", auth=(usuario, clave)).json()["token"]
H = {"Authorization": f"Bearer {token}"}

pedido = json.load(open(os.path.join(AQUI, "pedido_smap_appeears.json")))
tarea = requests.post(f"{API}/task", json=pedido, headers=H).json()
tid = tarea["task_id"]
print("Request sent:", tid)

while True:                                   # point requests usually take 5-30 min
    est = requests.get(f"{API}/task/{tid}", headers=H).json()["status"]
    print(time.strftime("%H:%M"), est)
    if est == "done": break
    if est == "error": raise SystemExit("AppEEARS reported an error; check the request in the website")
    time.sleep(60)

for f in requests.get(f"{API}/bundle/{tid}", headers=H).json()["files"]:
    if f["file_name"].endswith("-results.csv"):
        r = requests.get(f"{API}/bundle/{tid}/{f['file_id']}", headers=H, allow_redirects=True)
        ruta = os.path.join(DESTINO, os.path.basename(f["file_name"]))
        open(ruta, "wb").write(r.content)
        print("Saved:", ruta)
print("Done. Reload http://localhost:8000: SMAP should show ✅")
