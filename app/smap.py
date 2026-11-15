"""NASA SMAP L4 (SPL4SMGP, 9 км) через AppEEARS. Запрос асинхронный (минуты) -> идёт в фоне, результат кэшируется в файле."""
import os, io, csv, json, time, asyncio, threading
from datetime import date, timedelta
import httpx

BASE = "https://appeears.earthdatacloud.nasa.gov/api"
USER, PWD = os.getenv("EARTHDATA_USER", ""), os.getenv("EARTHDATA_PASS", "")
CACHE_FILE = os.path.join(os.path.dirname(os.getenv("DB_PATH", "/data/app.db")), "smap_cache.json")
REFRESH_AFTER = 12 * 3600      # обновлять не чаще раза в 12 ч
MAX_AGE = 5 * 86400            # старше 5 суток — не используем
SUMMER_REFRESH = 30 * 86400    # лето за 3 года обновляем раз в месяц
_lock = threading.Lock()
jobs: dict = {}

def enabled() -> bool:
    return bool(USER and PWD)

def key(lat, lon) -> str:
    return f"{round(lat, 1)},{round(lon, 1)}"   # сетка SMAP ~9 км

def _load() -> dict:
    try:
        with open(CACHE_FILE) as f: return json.load(f)
    except Exception:
        return {}

def cached(lat, lon):
    return _load().get(key(lat, lon))

def get_smap(lat, lon):
    """Мгновенно отдаёт кэш (или None) и при необходимости запускает обновление в фоне."""
    if not enabled(): return None
    v = cached(lat, lon)
    age = time.time() - v["ts"] if v else None
    if age is None or age > REFRESH_AFTER: start_job(lat, lon)
    return v if v and age < MAX_AGE else None

def get_summer(lat, lon):
    """Среднее летнее значение SMAP root zone за 3 года (кэш), иначе None + запуск фоновой загрузки."""
    if not enabled(): return None
    v = _load().get("S:" + key(lat, lon))
    if v is None or time.time() - v["ts"] > SUMMER_REFRESH: start_job(lat, lon, kind="summer")
    return v

def start_job(lat, lon, force=False, kind="now"):
    k = ("S:" if kind == "summer" else "") + key(lat, lon); j = jobs.get(k)
    if j and j["state"] == "running": return False
    if j and j["state"] == "error" and not force and time.time() - j["ts"] < 1800: return False  # не долбим API после ошибки
    jobs[k] = {"state": "running", "ts": time.time(), "msg": "запуск"}
    threading.Thread(target=lambda: asyncio.run(_fetch(lat, lon, kind)), daemon=True).start()
    return True

def _parse(text):
    rows = sorted(csv.DictReader(io.StringIO(text)), key=lambda r: r.get("Date", ""))
    if not rows: raise RuntimeError("пустой CSV от AppEEARS")
    cols = list(rows[0].keys())
    rc = next((c for c in cols if c.endswith("sm_rootzone")), None)
    sc = next((c for c in cols if c.endswith("sm_surface")), None)
    if not rc: raise RuntimeError(f"нет колонки sm_rootzone, есть: {cols}")
    def val(row, col):
        try:
            v = float(row[col]); return v if 0 <= v <= 1 else None   # -9999 = пропуск
        except Exception:
            return None
    for row in reversed(rows):
        root = val(row, rc)
        if root is not None:
            return root, (val(row, sc) if sc else None), row.get("Date")
    raise RuntimeError("в CSV нет валидных значений")

def _parse_summer(text, north):
    months = (6, 7, 8) if north else (12, 1, 2)
    rc, vals = None, []
    for r in csv.DictReader(io.StringIO(text)):
        if rc is None:
            rc = next((c for c in r if c.endswith("sm_rootzone")), None)
            if not rc: raise RuntimeError("нет колонки sm_rootzone")
        try:
            v = float(r[rc])
            if 0 <= v <= 1 and int(r["Date"][5:7]) in months: vals.append(v)
        except Exception:
            continue
    if len(vals) < 50: raise RuntimeError(f"мало летних значений: {len(vals)}")
    return sum(vals) / len(vals), len(vals)

async def _fetch(lat, lon, kind="now"):
    k = ("S:" if kind == "summer" else "") + key(lat, lon); la, lo = round(lat, 1), round(lon, 1)
    def note(m): jobs[k]["msg"] = m
    try:
        async with httpx.AsyncClient(timeout=90) as c:
            r = await c.post(f"{BASE}/login", auth=(USER, PWD))
            if r.status_code in (401, 403): raise RuntimeError("Earthdata: неверный логин/пароль")
            r.raise_for_status()
            h = {"Authorization": "Bearer " + r.json()["token"]}

            note("поиск продукта SPL4SMGP")
            prods = (await c.get(f"{BASE}/product")).json()
            cand = [p["ProductAndVersion"] for p in prods if p.get("ProductAndVersion", "").startswith("SPL4SMGP.")]
            if not cand: raise RuntimeError("SPL4SMGP не найден в AppEEARS")
            pv = max(cand, key=lambda x: int(x.split(".")[-1]) if x.split(".")[-1].isdigit() else 0)
            layers = (await c.get(f"{BASE}/product/{pv}")).json()
            names = [n for n in layers if n.endswith("sm_rootzone") or n.endswith("sm_surface")]
            if not names: raise RuntimeError(f"в {pv} нет слоёв sm_rootzone/sm_surface")

            end = date.today(); start = end - timedelta(days=10 if kind == "now" else 3 * 365)
            body = {"task_type": "point", "task_name": f"agro-{k}-{int(time.time())}", "params": {
                "dates": [{"startDate": start.strftime("%m-%d-%Y"), "endDate": end.strftime("%m-%d-%Y")}],
                "layers": [{"product": pv, "layer": n} for n in names],
                "coordinates": [{"id": "p", "longitude": lo, "latitude": la, "category": "field"}]}}
            r = await c.post(f"{BASE}/task", json=body, headers=h); r.raise_for_status()
            tid = r.json()["task_id"]

            for _ in range(90):   # до ~30 минут
                await asyncio.sleep(20)
                s = await c.get(f"{BASE}/task/{tid}", headers=h); s.raise_for_status()
                st = s.json().get("status"); note(f"задача {tid}: {st}")
                if st == "done": break
                if st not in ("pending", "processing", "queued", "running", None):
                    raise RuntimeError(f"задача AppEEARS завершилась со статусом {st}")
            else:
                raise TimeoutError("AppEEARS не успел за 30 минут")

            b = (await c.get(f"{BASE}/bundle/{tid}", headers=h)).json()
            files = [x for x in b["files"] if x.get("file_type") == "csv" and "results" in x["file_name"].lower()]
            if not files: raise RuntimeError("в результатах нет CSV")
            r = await c.get(f"{BASE}/bundle/{tid}/{files[0]['file_id']}", headers=h)
            if r.status_code in (301, 302, 303, 307, 308):   # редирект на S3 — без заголовка авторизации
                r = await c.get(r.headers["location"])
            r.raise_for_status()
            text = r.text

        if kind == "now":
            root, surf, d = _parse(text)
            entry, msg = {"ts": time.time(), "date": d, "root": root, "surface": surf, "product": pv}, f"ok, {d}"
        else:
            mean, n = _parse_summer(text, lat >= 0)
            entry, msg = {"ts": time.time(), "summer_root_mean": mean, "n": n, "years": 3, "product": pv}, f"ok, летних значений: {n}"
        with _lock:
            data = _load(); data[k] = entry
            with open(CACHE_FILE, "w") as f: json.dump(data, f)
        jobs[k] = {"state": "done", "ts": time.time(), "msg": msg}
    except Exception as e:
        jobs[k] = {"state": "error", "ts": time.time(), "msg": f"{type(e).__name__}: {e}"}
