# SmartFarm V1.1

NASA hackathon project — an AI-assisted smart-farming platform. Click a field on the map
and get an irrigation recommendation based on **NASA SMAP** satellite soil-moisture data
(fallback: **NASA POWER**, plus **Open-Meteo** for rain forecasts), diagnose plant-leaf
diseases from photos with YOLO models, get a crop-rotation strategy, a yield-impact
calculator, an AI agronomist chat, and an optional physical ESP32/Arduino soil-moisture
sensor that reports over HTTP.

UI language is Russian. Built with FastAPI + SQLite + a single-page frontend.
No Docker, no tunnels — deploys straight from this repository to Render.

## NASA data this app uses (all real, no placeholders)

| Source | What for | Key required? |
|---|---|---|
| **NASA SMAP L4 (SPL4SMGP)** via **AppEEARS** | Root-zone & surface soil moisture, 9 km grid, ~2–3 day latency. The SMAP data flow (`app/smap.py`) logs in to Earthdata, submits an AppEEARS task, polls it and caches the result (`data/smap_cache.json`). | Yes — free Earthdata login (below) |
| **NASA POWER** (`power.larc.nasa.gov`) | Daily GWETROOT/GWETTOP (profile saturation), automatic fallback when SMAP credentials are absent or SMAP has not answered yet; also 3-year summer history for crop strategy | No |
| **Open-Meteo** | 3-day forecast (precipitation, 0–1 cm and 27–81 cm soil moisture), ERA5-Land archive fallback | No |
| **SoilGrids (ISRIC)** | Clay/sand/SOC → USDA texture → water-holding constants used by the irrigation engine | No |

SMAP specifics: an AppEEARS request is asynchronous and takes **a few minutes**
(tens of minutes for the 3-summer history). The app never blocks on it: the first
request for a field returns NASA POWER/Open-Meteo data, SMAP loads in the background,
and subsequent requests are served from the SMAP cache. `GET /api/v1/smap/status?lat=..&lon=..`
shows the state; `POST /api/v1/smap/refresh` triggers a manual fetch.

## Deploy to Render (permanent, free)

1. Go to <https://dashboard.render.com/> and log in with GitHub.
2. Click **New +** in the top right → **Web Service**.
3. Select the repository `hopeapostrophe/SmartFarmV1.1`.
4. Fill in:
   - **Name:** `smartfarmai` (or anything)
   - **Runtime:** `Python 3`
   - **Build command:**
     `pip install -r requirements.txt && pip uninstall -y opencv-python && pip install --no-cache-dir opencv-python-headless`
   - **Start command:** `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
   - **Instance type:** `Free`
5. Add environment variables (**Advanced** → **Add Environment Variable** — or after creation
   under **Environment**), see the table below. At minimum set `EARTHDATA_USER`,
   `EARTHDATA_PASS`, `GEMINI_API_KEY`.
6. Click **Create Web Service**. The first build takes a few minutes (PyTorch is large).
   When it finishes, open the printed URL (e.g. `https://smartfarmai.onrender.com/health`
   returns `{"ok": true, ...}`).

`render.yaml` is included, so you can also use **New + → Blueprint** for the same result.

> **Free tier note:** the service spins down after ~15 minutes of no traffic. The first
> visit after idle takes ~30 seconds to wake up. Upgrading to the $7/month instance keeps
> it always hot.

> **Ephemeral disk:** Render's free tier does not persist the filesystem, so the SQLite
> history (`data/app.db`) and the SMAP cache (`data/smap_cache.json`) reset on every
> deploy/restart. This only affects history counters — sensor config, models and NASA
> credentials are unaffected. SMAP re-fetches itself on demand.

## Environment variables

Where to put them: **Render dashboard → Environment** (production), or a local `smap.env`
file (gitignored; auto-loaded via python-dotenv). `.env.example` documents every one of them.

| Variable | Required | Where to get it |
|---|---|---|
| `EARTHDATA_USER` / `EARTHDATA_PASS` | for SMAP | Free account at <https://urs.earthdata.nasa.gov>. After registering, **Authorize AppEEARS**: urs.earthdata.nasa.gov → Profile → Authorized Apps → "AppEEARS" → Authorize |
| `GEMINI_API_KEY` | for the AI assistant | Free key at <https://aistudio.google.com/apikey> |
| `SITE_USER` / `SITE_PASSWORD` | no | Any values — locks the whole site behind HTTP Basic auth (sensor ingest stays exempt) |
| `SENSOR_KEY` | no | Any long random string — protects `POST /api/v1/sensor/ingest` |
| `SENSOR_MODE` | no | `volumetric` (default, m³/m³) or `relative` (% saturation) |
| `SENSOR_RAW_DRY` / `SENSOR_RAW_WET` | no | Raw ADC calibration of your moisture sensor in air / in water (defaults `1023` / `300`) |
| `AI_BASE_URL`, `AI_MODEL`, `AI_FALLBACK_MODELS`, `AI_API_KEY` | no | Override the default Gemini OpenAI-compatible endpoint and model |
| `MODEL_PATH` / `MODEL_SEG_PATH` | no | Defaults to the included `models/best.pt` / `models/seg.pt` |
| `DB_PATH` | no | Default `./data/app.db` |
| `MAX_IMAGE_MB` / `MAX_VIDEO_MB` | no | Upload limits (`60` / `600`) |
| `DISEASE_KEYWORDS` / `SEG_LABEL` | no | Detection-tuning overrides |

`PORT` is provided by Render automatically; the app binds `0.0.0.0` and reads it
(`uvicorn ... --port $PORT`), locally it falls back to `8001`.

**Never commit `smap.env` or any real key.** They are gitignored here; if a key ever
lands in git history, rotate it.

## Run locally (optional — not needed for Render)

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows  (source .venv/bin/activate on macOS/Linux)
pip install -r requirements.txt
copy .env.example smap.env      # then edit smap.env with your values (gitignored)
python -m app.main              # or: uvicorn app.main:app --host 0.0.0.0 --port 8001
```

Open <http://localhost:8001>. `GET /health` reports model files, sensor and SMAP status.

## Physical soil sensor (Arduino / ESP32)

The board talks to the server over HTTP, so it works against the deployed URL too.

```bash
pip install pyserial        # if you don't have the venv above
python bridge\usb_bridge.py --url https://your-app.onrender.com --key YOUR_SENSOR_KEY
```

Windows: `bridge\start_windows.bat --url https://your-app.onrender.com` (auto-detects the
COM port and baud rate). See `bridge/arduino_example/arduino_example.ino` for the sketch.
The bridge is only a sensor gateway — it is not part of the deployed web service.

## Repository layout

```
app/
  main.py           # FastAPI app: endpoints, irrigation engine, SQLite, auth
  smap.py           # NASA SMAP L4 via AppEEARS (login -> task -> poll -> cache)
  vision.py         # YOLO detection: photos, batch, EXIF GPS, video, tiling
  soil.py           # USDA texture classes, hydraulics, crop table
  yield_calc.py     # Yield-impact calculator (router)
  static/index.html # Single-page frontend (Russian UI)
bridge/             # USB -> HTTP sensor bridge (runs on your PC, not on Render)
models/             # best.pt (disease), seg.pt (segmentation), best wilt.pt-3
training/           # How the YOLO models were trained (reference, not runtime)
render.yaml         # Render blueprint (optional; manual setup above works too)
.env.example        # Every variable with instructions (copy to smap.env)
```

## Notes

- The first SMAP fetch for a field takes minutes and happens in the background;
  everything else works immediately. `/health` shows `smap_enabled`.
- Models run on CPU (PyTorch CPU wheel). Free tier may be slow on YOLO inference —
  the rest of the app (NASA data, irrigation, AI chat, history) is unaffected.
- YOLO weights are included in this repository (`models/`). Ultralytics is AGPL-3.0 —
  keep that in mind if you redistribute commercially.
- License: MIT (see `LICENSE`).
