import io, os, re, json, time, base64, secrets, sqlite3, threading, asyncio
from uuid import uuid4
from datetime import datetime, timezone, date, timedelta
import httpx
from fastapi import FastAPI, Request, File, Form, Header, HTTPException, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, StreamingResponse
from PIL import Image

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_ROOT, ".env"))
    load_dotenv(os.path.join(_ROOT, "smap.env"))
except ImportError:
    pass
os.environ.setdefault("DB_PATH", os.path.join(_ROOT, "data", "app.db"))
os.environ.setdefault("MODEL_PATH", os.path.join(_ROOT, "models", "best.pt"))
os.environ.setdefault("MODEL_SEG_PATH", os.path.join(_ROOT, "models", "seg.pt"))

MODEL_PATH = os.getenv("MODEL_PATH", os.path.join(_ROOT, "models", "best.pt"))
DB_PATH = os.getenv("DB_PATH", os.path.join(_ROOT, "data", "app.db"))
SERIAL_PORT = os.getenv("SERIAL_PORT", "")
SENSOR_KEY = os.getenv("SENSOR_KEY", "")
SITE_USER, SITE_PASSWORD = os.getenv("SITE_USER", ""), os.getenv("SITE_PASSWORD", "")
SERIAL_BAUD = int(os.getenv("SERIAL_BAUD", "9600"))
RAW_DRY = float(os.getenv("SENSOR_RAW_DRY", "1023"))
RAW_WET = float(os.getenv("SENSOR_RAW_WET", "300"))
DISEASE_KEYS = os.getenv("DISEASE_KEYWORDS",
    "scab,rust,spot,blight,mold,mildew,virus,mosaic,curl,bacterial,rot,gray,mite,scorch,canker,wilt,yellow,necrosis,powdery,septoria").split(",")

from app import smap, vision
from app.soil import HYDRO, POROSITY, CROPS, usda_texture
from app.yield_calc import router as yield_router

app = FastAPI(title="Soil Monitoring & Strategy Platform")
app.include_router(yield_router)
# ---- AI-ассистент: прокси на OpenAI-совместимый API ----
# Ключ берётся либо из чата (пользователь вводит его в панели «⚙ API Keys»,
# хранится только в его браузере), либо из окружения сервера:
#   AI_BASE_URL        — например https://api.openai.com/v1 (по умолчанию — OpenAI-совместимый endpoint Gemini)
#   AI_MODEL           — основная модель (по умолчанию — gemini-3.6-flash)
#   AI_FALLBACK_MODELS — резервные модели через запятую (для Gemini-совместимого endpoint)
#   AI_API_KEY         — ключ; если не задан, используется GEMINI_API_KEY
AI_BASE_URL = os.getenv("AI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai").rstrip("/")
AI_MODEL = os.getenv("AI_MODEL", "gemini-3.6-flash").strip()
AI_FALLBACK_MODELS = [
    m.strip()
    for m in os.getenv(
        "AI_FALLBACK_MODELS",
        "gemini-3.5-flash,gemini-3.1-flash-lite,gemini-3-flash-preview",
    ).split(",")
    if m.strip()
]
AI_API_KEY = os.getenv("AI_API_KEY", "") or os.getenv("GEMINI_API_KEY", "")

_LOCAL_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "host.docker.internal")


def _is_local_base(url: str) -> bool:
    return any(host in url for host in _LOCAL_HOSTS)


def _json_response(payload: dict, status_code: int = 200) -> Response:
    return Response(
        json.dumps(payload, ensure_ascii=False),
        status_code=status_code,
        media_type="application/json",
    )


@app.get("/api/ai/config")
def ai_config():
    """Публичные настройки AI для фронтенда (без самого ключа)."""
    return {
        "base_url": AI_BASE_URL,
        "model": AI_MODEL,
        "server_key": bool(AI_API_KEY),
        "fallback_models": AI_FALLBACK_MODELS,
    }


@app.post("/api/chat")
async def chat_with_ai(request: Request):
    """Прокси к OpenAI-совместимому /chat/completions со стримингом.
    Фронт отправляет {base_url, api_key, model, temperature, messages, stream, max_tokens}.
    Если пользовательский ключ не передан — используется AI_API_KEY/GEMINI_API_KEY сервера.
    Для Gemini при 404/429/5xx автоматически пробуются резервные модели."""
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(422, "Ожидается JSON")
    if not isinstance(data, dict):
        raise HTTPException(422, "Ожидается JSON-объект")

    user_key = str(data.get("api_key") or "").strip()
    requested_base = str(data.get("base_url") or "").strip().rstrip("/")
    if requested_base and not (requested_base.startswith("http://") or requested_base.startswith("https://")):
        raise HTTPException(422, "Endpoint должен начинаться с http:// или https://")

    requested_model = str(data.get("model") or AI_MODEL).strip()
    if user_key:
        # BYOK: пользователь рискует только своим ключом — endpoint выбирает сам
        base_url = requested_base or AI_BASE_URL
        api_key = user_key
    elif requested_base and _is_local_base(requested_base):
        # Локальный OpenAI-совместимый сервер (LM Studio, Ollama) обычно без ключа
        base_url = requested_base
        api_key = ""
    else:
        # Ключ сервера используем только с серверным endpoint,
        # иначе можно увести AI_API_KEY на чужой хост.
        base_url = AI_BASE_URL
        api_key = AI_API_KEY
        if requested_base and requested_base != AI_BASE_URL:
            requested_model = AI_MODEL

    try:
        temperature = float(data.get("temperature", 0.7))
    except (TypeError, ValueError):
        temperature = 0.7
    temperature = max(0.0, min(2.0, temperature))

    messages = data.get("messages")
    if not isinstance(messages, list) or not messages:
        # Совместимость со старым виджетом: {message: "..."}
        msg = str(data.get("message", "")).strip()
        if not msg:
            raise HTTPException(422, "Пустое сообщение")
        messages = [{"role": "user", "content": msg}]
    messages = [
        m for m in messages
        if isinstance(m, dict) and m.get("role") in ("system", "user", "assistant")
    ][-20:]

    if not api_key and not _is_local_base(base_url):
        return _json_response(
            {"detail": "Ключ API не задан. Откройте «⚙ API Keys» в ассистенте и вставьте ваш ключ, либо задайте AI_API_KEY / GEMINI_API_KEY на сервере."},
            401,
        )

    stream = bool(data.get("stream", True))
    payload_base = {"messages": messages, "temperature": temperature}
    max_tokens = data.get("max_tokens")
    if isinstance(max_tokens, int) and 0 < max_tokens <= 4096:
        payload_base["max_tokens"] = max_tokens

    # Для Gemini добавляем резервные модели: 3.8-flash периодически перегружен.
    is_gemini = "generativelanguage.googleapis.com" in base_url
    models = [requested_model]
    if is_gemini:
        for m in AI_FALLBACK_MODELS:
            if m not in models:
                models.append(m)

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key

    upstream = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0))
    resp = None
    used_model = requested_model
    last_error = "Не удалось получить ответ от API"
    try:
        for model in models:
            used_model = model
            payload = dict(payload_base)
            payload["model"] = model
            payload["stream"] = stream
            try:
                req = upstream.build_request(
                    "POST",
                    base_url + "/chat/completions",
                    json=payload,
                    headers=headers,
                )
                candidate = await asyncio.wait_for(upstream.send(req, stream=True), timeout=15.0)
            except asyncio.TimeoutError:
                last_error = f"Модель {model} не ответила за 15 секунд."
                continue
            except Exception as e:
                last_error = f"Не удалось связаться с API ({base_url}): {e}"
                continue

            if candidate.status_code in (200, 201):
                resp = candidate
                break

            status_code = candidate.status_code
            body = (await candidate.aread()).decode("utf-8", "replace")[:700]
            await candidate.aclose()
            last_error = f"API вернул HTTP {status_code}: {body}"
            # 404/429/5xx и таймауты — повод попробовать следующую модель.
            if status_code not in (404, 408, 409, 425, 429, 500, 502, 503, 504):
                break

        if resp is None:
            await upstream.aclose()
            return _json_response({"detail": last_error}, 502)

        if not stream:
            body = await resp.aread()
            content_type = resp.headers.get("content-type", "application/json")
            await resp.aclose()
            await upstream.aclose()
            return Response(
                content=body,
                status_code=200,
                media_type=content_type,
                headers={"X-AI-Model": used_model},
            )

        async def relay():
            try:
                async for chunk in resp.aiter_bytes():
                    yield chunk
            finally:
                await resp.aclose()
                await upstream.aclose()

        return StreamingResponse(
            relay(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-AI-Model": used_model,
            },
        )
    except Exception as e:
        try:
            await upstream.aclose()
        except Exception:
            pass
        return _json_response({"detail": f"Ошибка прокси: {e}"}, 502)
@app.middleware("http")
async def site_auth(request, call_next):
    """Если задан SITE_PASSWORD, весь сайт и API закрыты логином и паролем (браузер покажет окно входа).
    Исключение: приём показаний датчика, он защищается отдельным SENSOR_KEY."""
    if SITE_PASSWORD and request.url.path != "/api/v1/sensor/ingest":
        ok, h = False, request.headers.get("authorization", "")
        if h.lower().startswith("basic "):
            try:
                u, _, pw = base64.b64decode(h[6:]).decode().partition(":")
                ok = secrets.compare_digest(u.encode(), SITE_USER.encode()) and secrets.compare_digest(pw.encode(), SITE_PASSWORD.encode())
            except Exception:
                ok = False
        if not ok:
            return Response("Нужен вход", status_code=401, headers={"WWW-Authenticate": 'Basic realm="Smart Farming AI"'})
    return await call_next(request)

@app.get("/", include_in_schema=False)
def index():
    return FileResponse(os.path.join(os.path.dirname(__file__), "static", "index.html"))

# ---------------- БД (SQLite, папка data/) ----------------
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
db = sqlite3.connect(DB_PATH, check_same_thread=False)
lock = threading.Lock()
with lock:
    db.execute("CREATE TABLE IF NOT EXISTS sensor(ts REAL, moisture REAL, raw TEXT)")
    db.execute("CREATE TABLE IF NOT EXISTS history(ts REAL, lat REAL, lon REAL, surface REAL, root REAL, status TEXT)")
    db.execute("CREATE TABLE IF NOT EXISTS fields(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, lat REAL, lon REAL, crop TEXT, last TEXT, updated REAL)")
    db.commit()

# ---------------- USB / Serial (физ. модель) ----------------
sensor = {"last": None, "ts": 0.0, "error": None}

SENSOR_MODE = os.getenv("SENSOR_MODE", "volumetric")   # relative: проценты/сырые значения = доля насыщения почвы

def norm_moisture(v: float) -> float:
    """Показание датчика -> м3/м3. До 1: доля; до 100: проценты; больше 100: сырой ADC (по калибровке сухо/мокро)."""
    if v <= 1: frac = v
    elif v <= 100: frac = v / 100
    else: frac = max(0.0, min(1.0, (RAW_DRY - v) / (RAW_DRY - RAW_WET)))
    if SENSOR_MODE == "relative": return frac * 0.45
    return frac * (0.5 if v > 100 else 1)

def parse_line(line: str) -> dict:
    """JSON, пары «ключ: число» (латиница или кириллица, пробелы в ключе допустимы) или просто число."""
    try:
        d = json.loads(line)
        if isinstance(d, dict):
            return {str(k).lower(): float(v) for k, v in d.items() if isinstance(v, (int, float))}
    except Exception:
        pass
    out = {"_".join(k.lower().split()): float(v.replace(",", "."))
           for k, v in re.findall(r"([A-Za-zА-Яа-яЁё_ ]+?)\s*[:=]\s*(-?\d+(?:[.,]\d+)?)", line)}
    if not out and re.fullmatch(r"\s*-?\d+(?:[.,]\d+)?\s*", line):
        out["moisture"] = float(line.replace(",", "."))
    return out

def pick_moisture(data: dict):
    for k in ("moisture", "soil_moisture"):
        if k in data: return data[k]
    for k, v in data.items():
        if any(t in k for t in ("moist", "влаж", "soil", "почв")): return v
    return None

_last_saved = [0.0]
def handle_reading(raw, source):
    """Строка или словарь с датчика -> обновляет текущее показание. Возвращает данные или None."""
    data = parse_line(raw) if isinstance(raw, str) else {k.lower(): float(v) for k, v in raw.items()}
    m = pick_moisture(data)
    if m is None: return None
    data["moisture_m3m3"] = round(norm_moisture(m), 4)
    sensor.update(last=data, ts=time.time(), source=source, error=None)
    if time.time() - _last_saved[0] > 30:
        with lock:
            db.execute("INSERT INTO sensor VALUES(?,?,?)", (time.time(), data["moisture_m3m3"], json.dumps(raw, ensure_ascii=False)))
            db.commit()
        _last_saved[0] = time.time()
    return data

def serial_loop():
    import serial
    while True:
        try:
            with serial.Serial(SERIAL_PORT, SERIAL_BAUD, timeout=2) as s:
                sensor["error"] = None
                while True:
                    line = s.readline().decode(errors="ignore").strip()
                    if line: handle_reading(line, "serial")
        except Exception as e:
            sensor["error"] = str(e)
            time.sleep(5)

@app.on_event("startup")
def startup():
    threading.Thread(target=refresh_loop, daemon=True).start()
    if SERIAL_PORT:
        threading.Thread(target=serial_loop, daemon=True).start()

def fresh_sensor():
    if sensor["last"] and time.time() - sensor["ts"] < 300:
        return sensor["last"]
    return None

# ---------------- Внешние данные ----------------
_cache = {}
def cached(key, ttl=3 * 3600):
    v = _cache.get(key)
    return v[1] if v and time.time() - v[0] < ttl else None
def put(key, val):
    _cache[key] = (time.time(), val); return val


async def fetch_nasa_power(lat, lon, days=10):
    """NASA POWER (бесплатно, без ключа): GWETROOT/GWETTOP — влажность почвы как доля насыщения (0..1). Задержка ~2-3 дня."""
    key = ("power", round(lat, 2), round(lon, 2))
    if (hit := cached(key)): return hit
    end = date.today(); start = end - timedelta(days=days)
    try:
        async with httpx.AsyncClient(timeout=40) as c:
            r = await c.get("https://power.larc.nasa.gov/api/temporal/daily/point", params={
                "parameters": "GWETROOT,GWETTOP", "community": "AG", "latitude": lat, "longitude": lon,
                "start": start.strftime("%Y%m%d"), "end": end.strftime("%Y%m%d"), "format": "JSON"})
            r.raise_for_status()
        p = r.json()["properties"]["parameter"]
        def last(name):
            for d in sorted(p[name], reverse=True):
                if p[name][d] != -999: return p[name][d], d
            return None, None
        root, d = last("GWETROOT"); top, _ = last("GWETTOP")
        return put(key, {"root_frac": root, "surface_frac": top, "date": d}) if root is not None else None
    except Exception:
        return None

async def fetch_moisture(lat, lon):
    """Open-Meteo: прогноз осадков (+ запасной источник влажности). Не падает при сбое."""
    key = ("om", round(lat, 2), round(lon, 2))
    if (hit := cached(key, 3600)): return hit
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get("https://api.open-meteo.com/v1/forecast", params={
                "latitude": lat, "longitude": lon, "forecast_days": 3, "timezone": "UTC",
                "hourly": "soil_moisture_0_to_1cm,soil_moisture_27_to_81cm,precipitation"})
            r.raise_for_status()
        h = r.json()["hourly"]; i = datetime.now(timezone.utc).hour
        return put(key, {"surface": h["soil_moisture_0_to_1cm"][i], "root": h["soil_moisture_27_to_81cm"][i],
                         "rain_48h_mm": round(sum(x or 0 for x in h["precipitation"][i:i + 48]), 1)})
    except Exception:
        return {"surface": None, "root": None, "rain_48h_mm": 0.0}

async def fetch_soil(lat, lon):
    """SoilGrids (бывает медленным): 2 попытки, успешный ответ кэшируется на 30 суток; иначе суглинок (assumed=True)."""
    ck = ("soil", round(lat, 2), round(lon, 2))
    if (hit := cached(ck, 30 * 86400)): return hit
    for _ in range(2):
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.get("https://rest.isric.org/soilgrids/v2.0/properties/query", params=[
                    ("lat", lat), ("lon", lon), ("property", "clay"), ("property", "sand"),
                    ("property", "soc"), ("depth", "0-5cm"), ("value", "mean")])
                r.raise_for_status()
            v = {l["name"]: l["depths"][0]["values"]["mean"] for l in r.json()["properties"]["layers"]}
            clay, sand, soc = v["clay"] / 10, v["sand"] / 10, v["soc"] / 10
            tex = usda_texture(sand, clay)
            return put(ck, {"clay_pct": round(clay, 1), "sand_pct": round(sand, 1), "soc_g_kg": round(soc, 1), "texture": tex, "assumed": False})
        except Exception:
            await asyncio.sleep(2)
    return {"clay_pct": None, "sand_pct": None, "soc_g_kg": None, "texture": "loam", "assumed": True}

async def fetch_history_om(lat, lon, years=3):
    """ERA5-Land через Open-Meteo archive: влажность 28–100 см за N лет."""
    end = date.today() - timedelta(days=7); start = end.replace(year=end.year - years)
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.get("https://archive-api.open-meteo.com/v1/archive", params={
                "latitude": lat, "longitude": lon, "start_date": start.isoformat(), "end_date": end.isoformat(),
                "hourly": "soil_moisture_28_to_100cm", "timezone": "UTC"})
            r.raise_for_status()
        h = r.json()["hourly"]
        summer = (6, 7, 8) if lat >= 0 else (12, 1, 2)
        vals = [v for t, v in zip(h["time"], h["soil_moisture_28_to_100cm"]) if v is not None and int(t[5:7]) in summer]
        return sum(vals) / len(vals) if vals else None
    except Exception:
        return None

async def fetch_history(lat, lon, soil, years=3):
    """Среднее летнее значение влажности корневой зоны (м3/м3) за N лет: NASA POWER, запасной — ERA5."""
    summer = (6, 7, 8) if lat >= 0 else (12, 1, 2)
    end = date.today() - timedelta(days=5); start = end - timedelta(days=365 * years)
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.get("https://power.larc.nasa.gov/api/temporal/daily/point", params={
                "parameters": "GWETROOT", "community": "AG", "latitude": lat, "longitude": lon,
                "start": start.strftime("%Y%m%d"), "end": end.strftime("%Y%m%d"), "format": "JSON"})
            r.raise_for_status()
        vals = [v for d, v in r.json()["properties"]["parameter"]["GWETROOT"].items()
                if v != -999 and int(d[4:6]) in summer]
        if vals: return sum(vals) / len(vals) * POROSITY[soil["texture"]]
    except Exception:
        pass
    return await fetch_history_om(lat, lon, years)

# ---------------- Рекомендательный движок ----------------
def irrigation(root, soil, crop, rain):
    pwp, fc = HYDRO[soil["texture"]]
    depth_mm = CROPS.get(crop, CROPS["wheat"])[0] * 1000
    r = (root - pwp) / (fc - pwp)
    need = 0.0
    if r > 1.1: status, color = "WATERLOGGING_RISK", "blue"
    elif r >= 0.5: status, color = "OPTIMAL", "green"
    else:
        need = max(0.0, (pwp + 0.75 * (fc - pwp) - root) * depth_mm - rain * 0.8)
        if need == 0: status, color = "DRY_RAIN_EXPECTED", "yellow"
        elif r >= 0.3: status, color = "ACTION_REQUIRED", "yellow"
        else: status, color = "CRITICAL", "red"
    msgs = {"WATERLOGGING_RISK": "Почва переувлажнена — полив не нужен, риск застоя воды.",
            "OPTIMAL": "Влажность оптимальна, полив не требуется.",
            "DRY_RAIN_EXPECTED": "Суховато, но ожидаемые осадки за 48 ч покроют дефицит.",
            "ACTION_REQUIRED": f"Рекомендуется полив ≈ {round(need)} мм.",
            "CRITICAL": f"Срочный полив ≈ {round(need)} мм!"}
    return {"status": status, "color": color, "recommended_water_mm": round(need),
            "liters_per_m2": round(need), "relative_moisture": round(r, 2), "message": msgs[status]}

def crop_strategy(summer_root, soil, prev_crop):
    pwp, fc = HYDRO[soil["texture"]]
    if summer_root is None: return [], None
    r = (summer_root - pwp) / (fc - pwp)
    need = 5 if r < 0.3 else 4 if r < 0.45 else 3 if r < 0.6 else 2 if r < 0.8 else 1
    low_soc = soil["soc_g_kg"] is not None and soil["soc_g_kg"] < 10
    prev_legume = CROPS.get(prev_crop, (0, 0, True))[2] if prev_crop else True
    out = []
    for name, (_, tol, legume) in CROPS.items():
        s = 90 - 22 * max(0, need - tol) - 3 * max(0, tol - need)
        reasons = [f"засухоустойчивость {tol}/5 при летнем дефиците {need}/5"]
        if legume and (not prev_legume or low_soc):
            s += 10; reasons.append("бобовая — вернёт азот в почву")
        if prev_crop and name == prev_crop:
            s -= 25; reasons.append("повтор культуры — нарушает севооборот")
        out.append({"crop": name, "score": max(0, min(100, round(s))), "reason": "; ".join(reasons)})
    return sorted(out, key=lambda x: -x["score"])[:5], round(r, 2)

# ---------------- Эндпоинты ----------------
@app.get("/health")
def health():
    return {"ok": True, "models": {k: os.path.exists(v) for k, v in vision.MODELS.items()}, "serial_port": SERIAL_PORT or None,
            "sensor_error": sensor["error"], "sensor_fresh": fresh_sensor() is not None, "smap_enabled": smap.enabled()}

@app.get("/api/v1/models")
def models_info():
    """Какие классы знает каждая модель (с русскими названиями)."""
    out = {}
    for k, pth in vision.MODELS.items():
        info = {"exists": os.path.exists(pth)}
        if info["exists"]:
            try:
                names = list(vision.get_model(k).names.values())
                info["count"] = len(names)
                info["classes"] = [{"name": n, "ru": vision.ru_name(n), "disease": vision.is_disease(n) if k == "disease" else None} for n in names]
            except Exception as e:
                info["error"] = str(e)
        out[k] = info
    return out

@app.get("/api/v1/sensor")
def get_sensor():
    """Последнее показание физической модели (по USB напрямую или через usb-мост)."""
    return {"fresh": fresh_sensor() is not None, "source": sensor.get("source"),
            "age_s": round(time.time() - sensor["ts"], 1) if sensor["ts"] else None,
            "data": sensor["last"], "error": sensor["error"]}

@app.get("/api/v1/sensor/history")
def sensor_history(limit: int = 200):
    with lock:
        rows = db.execute("SELECT ts, moisture FROM sensor ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
    return [{"ts": t, "moisture_m3m3": m} for t, m in rows]

async def build_status(lat, lon, crop, texture=None):
    user_soil = {"clay_pct": None, "sand_pct": None, "soc_g_kg": None, "texture": texture, "assumed": False, "user_defined": True} if texture in HYDRO else None
    async def _soil(): return user_soil or await fetch_soil(lat, lon)
    wx, soil, nasa = await asyncio.gather(fetch_moisture(lat, lon), _soil(), fetch_nasa_power(lat, lon))
    por, sens, sm = POROSITY[soil["texture"]], fresh_sensor(), smap.get_smap(lat, lon)
    if sens: root, surface, src = sens["moisture_m3m3"], wx["surface"], "usb_sensor"
    elif sm: root, surface, src = sm["root"], sm["surface"] if sm["surface"] is not None else wx["surface"], f"nasa-smap-L4 ({sm['date']})"
    elif nasa: root, surface, src = nasa["root_frac"] * por, (nasa["surface_frac"] or 0) * por, f"nasa-power ({nasa['date']})"
    elif wx["root"] is not None: root, surface, src = wx["root"], wx["surface"], "open-meteo"
    else: raise HTTPException(502, "Нет данных о влажности: NASA POWER и Open-Meteo недоступны")
    wx = {**wx, "surface": surface}
    irr = irrigation(root, soil, crop, wx["rain_48h_mm"])
    with lock:
        db.execute("INSERT INTO history VALUES(?,?,?,?,?,?)", (time.time(), lat, lon, surface, root, irr["status"]))
        db.commit()
    return wx, soil, round(root, 4), src, irr

@app.get("/api/v1/field-analytics")
async def field_analytics(lat: float, lon: float, crop_type: str = "wheat", previous_crop: str | None = None,
                          soil_texture: str | None = None):
    """soil_texture (необязательно): sandy | sandy_loam | loam | clay — задать вручную, если SoilGrids недоступен."""
    wx, soil, root, src, irr = await build_status(lat, lon, crop_type, soil_texture)
    sm = smap.get_summer(lat, lon)
    if sm:
        summer, hist_src = sm["summer_root_mean"], f"nasa-smap-L4 (лето, {sm['years']} г.)"
    else:
        summer = await fetch_history(lat, lon, soil)
        hist_src = "nasa-power (запасной, менее точный; SMAP за 3 лета загружается)" if smap.enabled() else "nasa-power"
    crops, summer_rel = crop_strategy(summer, soil, previous_crop)
    month = datetime.now(timezone.utc).month
    notes = []
    if soil["assumed"]:
        notes.append("SoilGrids недоступен — тип почвы принят как суглинок. Укажите soil_texture вручную.")
    if (lat >= 0 and month in (11, 12, 1, 2)) or (lat < 0 and month in (5, 6, 7, 8)):
        notes.append("Сейчас вне вегетационного сезона большинства культур: совет по поливу актуален весной–летом.")
    return {
        "location": {"lat": lat, "lon": lon},
        "current_status": {"surface_moisture_m3m3": wx["surface"], "root_moisture_m3m3": root,
                           "root_moisture_source": src, "open_meteo_root_moisture_m3m3": wx["root"],
                           "rain_next_48h_mm": wx["rain_48h_mm"], "soil": soil},
        "irrigation_advice": irr,
        "future_strategy": {"summer_relative_moisture": summer_rel, "history_source": hist_src,
                            "recommended_next_crops": crops,
                            "note": None if crops else "Нет исторических данных — стратегия недоступна"},
        "notes": notes,
    }

def _check_model(model):
    if model not in vision.MODELS: raise HTTPException(422, "model: disease или seg")

@app.post("/api/v1/detect")
async def detect(file: UploadFile = File(...), lat: float | None = Form(None), lon: float | None = Form(None),
                 crop_type: str = Form("wheat"), model: str = Form("disease"), conf: float | None = Form(None),
                 iou: float = Form(0.5), augment: bool = Form(False), annotate: bool = Form(False),
                 tile: bool = Form(False), imgsz: int = Form(640)):
    """Снимок JPEG/PNG (дрон, смартфон, камера) -> YOLO. tile=true: крупный снимок режется на фрагменты.
    Если lat/lon не заданы, берутся из GPS в метаданных снимка. Для модели disease результат сверяется с влажностью."""
    _check_model(model)
    try:
        img, gps = vision.load_image(await file.read())
        res = await run_in_threadpool(vision.detect_image, img, model, conf, iou, augment, annotate, tile, imgsz)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except FileNotFoundError as e:
        raise HTTPException(500, str(e))
    res["image"]["gps"] = gps
    if (lat is None or lon is None) and gps:
        lat, lon, res["location_source"] = gps["lat"], gps["lon"], "exif"
    if lat is not None and lon is not None and model == "disease":
        _, _, root, src, irr = await build_status(lat, lon, crop_type)
        dry = irr["status"] in ("ACTION_REQUIRED", "CRITICAL")
        dry_note = " Почва сейчас сухая: возможен и стресс от нехватки воды." if dry else ""
        if res["diseased"]:
            dx = ("Есть и дефицит влаги, и признаки болезни: полейте и проверьте лист, засуха ослабляет растение." if dry else
                  "Влажность в норме, но на растении признаки болезни: причина не в засухе, нужна диагностика или обработка.")
        elif not res["detections"]:
            crops = await run_in_threadpool(vision.known_crops, model)
            dx = ("Модель ничего не распознала. Это не значит, что лист здоров: она обучена только на части культур"
                  + (" (" + ", ".join(crops) + ")" if crops else "") + ", поэтому другую культуру или болезнь могла не узнать." + dry_note)
        else:
            dx = "Модель видит здоровый лист." + dry_note
        res["diagnosis"] = {"text": dx, "moisture_status": irr["status"], "moisture_source": src}
    return res

@app.post("/api/v1/detect/batch")
async def detect_batch(files: list[UploadFile] = File(...), model: str = Form("disease"),
                       conf: float | None = Form(None), tile: bool = Form(True)):
    """Несколько снимков с дрона за раз (до 50): итог по каждому, GPS из метаданных и общие суммы."""
    _check_model(model)
    out, totals = [], {}
    for f in files[:50]:
        try:
            img, gps = vision.load_image(await f.read())
            r = await run_in_threadpool(vision.detect_image, img, model, conf, 0.5, False, False, tile, 640)
        except Exception as e:
            out.append({"file": f.filename, "error": str(e)}); continue
        for k, v in r["counts"].items(): totals[k] = totals.get(k, 0) + v
        out.append({"file": f.filename, "gps": gps, "size": r["image"], "found": len(r["detections"]),
                    "diseased": len(r["diseased"]), "counts": r["counts"], "top": r["detections"][:3]})
    return {"model": model, "files": out, "totals": totals, "with_gps": sum(1 for o in out if o.get("gps"))}

@app.post("/api/v1/detect/video")
async def detect_video(file: UploadFile = File(...), model: str = Form("disease"), conf: float | None = Form(None),
                       iou: float = Form(0.5), stride: int = Form(3), max_seconds: float = Form(60),
                       track: bool = Form(True), save_video: bool = Form(False), imgsz: int = Form(640)):
    """Видео -> фоновая задача. stride: анализировать каждый N-й кадр; max_seconds: сколько секунд обработать (0 = всё)."""
    _check_model(model)
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in (".mp4", ".avi", ".mov", ".mkv", ".m4v", ".webm"):
        raise HTTPException(400, "Нужно видео MP4, AVI, MOV, MKV или WEBM")
    path, size = os.path.join(vision.VIDEO_DIR, f"up_{uuid4().hex}{ext}"), 0
    with open(path, "wb") as f:
        while chunk := await file.read(1 << 20):
            size += len(chunk)
            if size > vision.MAX_VIDEO_MB * 2**20:
                f.close(); os.remove(path); raise HTTPException(413, f"Видео больше {vision.MAX_VIDEO_MB} МБ")
            f.write(chunk)
    jid = vision.start_video_job(path, model, conf, iou, stride, max_seconds, track, save_video, imgsz)
    return {"job_id": jid, "status_url": f"/api/v1/jobs/{jid}"}

@app.get("/api/v1/jobs/{jid}")
def job_status(jid: str):
    j = vision.jobs.get(jid)
    if not j: raise HTTPException(404, "Задача не найдена (или устарела)")
    return j

@app.get("/api/v1/jobs/{jid}/video")
def job_video(jid: str):
    p = os.path.join(vision.VIDEO_DIR, f"{jid}.mp4")
    if jid not in vision.jobs or not os.path.exists(p): raise HTTPException(404, "Видео с разметкой не готово (нужен save_video=true)")
    return FileResponse(p, media_type="video/mp4", filename=f"analysis_{jid}.mp4")

@app.post("/api/v1/sensor/ingest")
async def sensor_ingest(payload: dict, x_sensor_key: str | None = Header(None)):
    """Приём показаний от usb-моста с Mac/ПК или любого устройства: {"line": "moisture:34.5"} или {"moisture": 34.5}."""
    if SENSOR_KEY and x_sensor_key != SENSOR_KEY: raise HTTPException(401, "Неверный ключ датчика")
    raw = payload["line"] if isinstance(payload.get("line"), str) else {k: v for k, v in payload.items() if isinstance(v, (int, float))}
    d = handle_reading(raw, "usb-bridge")
    if d is None: raise HTTPException(422, "Не нашёл влажность в строке. Ожидается, например, «moisture: 45» или «Влажность: 45».")
    return {"ok": True, "moisture_m3m3": d["moisture_m3m3"]}

# ---------------- Сохранённые поля + автообновление каждые 6 ч ----------------
def save_field(fid, root, src, irr):
    info = json.dumps({"root_moisture_m3m3": root, "source": src, **irr}, ensure_ascii=False)
    with lock:
        db.execute("UPDATE fields SET last=?, updated=? WHERE id=?", (info, time.time(), fid)); db.commit()

def refresh_loop():
    while True:
        with lock:
            rows = db.execute("SELECT id, lat, lon, crop FROM fields").fetchall()
        for fid, lat, lon, crop in rows:
            try:
                _, _, root, src, irr = asyncio.run(build_status(lat, lon, crop))
                save_field(fid, root, src, irr)
            except Exception:
                pass
        time.sleep(6 * 3600)

@app.post("/api/v1/fields")
async def add_field(name: str, lat: float, lon: float, crop_type: str = "wheat"):
    """Сохранить поле: оно будет обновляться само каждые 6 часов."""
    with lock:
        cur = db.execute("INSERT INTO fields(name, lat, lon, crop) VALUES(?,?,?,?)", (name, lat, lon, crop_type)); db.commit()
        fid = cur.lastrowid
    _, _, root, src, irr = await build_status(lat, lon, crop_type)
    save_field(fid, root, src, irr)
    return {"id": fid, "status": irr}

@app.get("/api/v1/fields")
def list_fields():
    with lock:
        rows = db.execute("SELECT id, name, lat, lon, crop, last, updated FROM fields").fetchall()
    return [{"id": i, "name": n, "lat": la, "lon": lo, "crop": c, "last": json.loads(l) if l else None, "updated_ts": u}
            for i, n, la, lo, c, l, u in rows]

@app.get("/api/v1/fields/{fid}/history")
def field_history(fid: int, limit: int = 200):
    with lock:
        f = db.execute("SELECT lat, lon FROM fields WHERE id=?", (fid,)).fetchone()
        if not f: raise HTTPException(404, "Поле не найдено")
        rows = db.execute("SELECT ts, root, status FROM history WHERE lat=? AND lon=? ORDER BY ts DESC LIMIT ?", (*f, limit)).fetchall()
    return [{"ts": t, "root_moisture_m3m3": r, "status": st} for t, r, st in rows]

# ---------------- NASA SMAP ----------------
@app.get("/api/v1/smap/status")
def smap_status(lat: float, lon: float):
    """Состояние SMAP-кэша и фоновой задачи для точки."""
    k = smap.key(lat, lon)
    return {"enabled": smap.enabled(), "cached": smap.cached(lat, lon), "job": smap.jobs.get(k),
            "summer_cached": smap._load().get("S:" + k), "summer_job": smap.jobs.get("S:" + k)}

@app.post("/api/v1/smap/refresh")
def smap_refresh(lat: float, lon: float):
    """Запустить загрузку SMAP вручную (занимает несколько минут)."""
    if not smap.enabled(): raise HTTPException(400, "Не заданы EARTHDATA_USER / EARTHDATA_PASS (файл smap.env или переменные окружения)")
    return {"started": smap.start_job(lat, lon, force=True)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8001)))
