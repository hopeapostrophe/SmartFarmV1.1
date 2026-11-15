"""YOLO: снимки (в т.ч. с дронов и смартфонов, с нарезкой на фрагменты), GPS из EXIF, видео с трекингом в фоне."""
import io, os, time, uuid, base64, threading
import numpy as np
from PIL import Image, ImageOps, ImageDraw, ImageFont

MODELS = {"disease": os.getenv("MODEL_PATH", "/models/best.pt"), "seg": os.getenv("MODEL_SEG_PATH", "/models/seg.pt")}
DEFAULT_CONF = {"disease": 0.25, "seg": 0.5}
SEG_LABEL = os.getenv("SEG_LABEL", "")   # как называть безымянный «Class 0» новой модели
DISEASE_KEYS = os.getenv("DISEASE_KEYWORDS",
    "scab,rust,spot,blight,mold,mildew,virus,mosaic,curl,bacterial,rot,gray,mite,scorch,canker,wilt,yellow,necrosis,powdery,septoria").split(",")
DATA_DIR = os.path.dirname(os.getenv("DB_PATH", "/data/app.db"))
VIDEO_DIR = os.path.join(DATA_DIR, "videos"); os.makedirs(VIDEO_DIR, exist_ok=True)
MAX_IMAGE_MB = int(os.getenv("MAX_IMAGE_MB", "60"))
MAX_VIDEO_MB = int(os.getenv("MAX_VIDEO_MB", "600"))
SEG_WARNING = "Модель сегментации пока слабая (mAP50 около 0,005 на её валидации): много ложных срабатываний. Повышайте conf."

_models, _mlock = {}, threading.Lock()
infer_lock = threading.Lock()          # одна инференс-операция за раз (CPU)
video_sem = threading.Semaphore(1)     # одно видео за раз
jobs: dict = {}

def get_model(name, purpose="img"):
    key = (name, purpose)
    with _mlock:
        if key not in _models:
            from ultralytics import YOLO
            path = MODELS[name]
            if not os.path.exists(path): raise FileNotFoundError(f"Нет файла модели {path}")
            _models[key] = YOLO(path)
        return _models[key]

def is_disease(n):
    n = n.lower()
    return "healthy" not in n and any(k in n for k in DISEASE_KEYS)

def _k(n): return " ".join(n.replace("_", " ").lower().split())
_RU = {
 "Apple Scab Leaf": "Яблоня: парша", "Apple leaf": "Яблоня: здоровый лист", "Apple rust leaf": "Яблоня: ржавчина",
 "Bell_pepper leaf spot": "Перец: пятнистость листа", "Bell_pepper leaf": "Перец: здоровый лист",
 "Blueberry leaf": "Голубика: здоровый лист", "Cherry leaf": "Вишня: здоровый лист",
 "Corn Gray leaf spot": "Кукуруза: серая пятнистость", "Corn leaf blight": "Кукуруза: ожог листа", "Corn rust leaf": "Кукуруза: ржавчина",
 "Peach leaf": "Персик: здоровый лист", "Potato leaf early blight": "Картофель: альтернариоз", "Potato leaf late blight": "Картофель: фитофтороз",
 "Potato leaf": "Картофель: здоровый лист", "Raspberry leaf": "Малина: здоровый лист", "Soyabean leaf": "Соя: здоровый лист", "Soybean leaf": "Соя: здоровый лист",
 "Squash Powdery mildew leaf": "Тыква: мучнистая роса", "Strawberry leaf": "Клубника: здоровый лист",
 "Tomato Early blight leaf": "Томат: альтернариоз", "Tomato Septoria leaf spot": "Томат: септориоз", "Tomato leaf bacterial spot": "Томат: бактериальная пятнистость",
 "Tomato leaf late blight": "Томат: фитофтороз", "Tomato leaf mosaic virus": "Томат: вирус мозаики", "Tomato leaf yellow virus": "Томат: вирус жёлтой курчавости",
 "Tomato leaf": "Томат: здоровый лист", "Tomato mold leaf": "Томат: плесень листьев", "Tomato two spotted spider mites leaf": "Томат: паутинный клещ",
 "grape leaf black rot": "Виноград: чёрная гниль", "grape leaf": "Виноград: здоровый лист",
 "Wheat_Brown_Rust": "Пшеница: бурая ржавчина", "Wheat_Yellow_Rust": "Пшеница: жёлтая ржавчина", "Wheat_Black_Rust": "Пшеница: стеблевая ржавчина",
 "Wheat_Septoria": "Пшеница: септориоз", "Wheat_Mildew": "Пшеница: мучнистая роса", "Wheat_Blast": "Пшеница: блэст (пирикуляриоз)",
 "Wheat_Fusarium_Head_Blight": "Пшеница: фузариоз колоса", "Wheat_Common_Root_Rot": "Пшеница: корневая гниль", "Wheat_Healthy": "Пшеница: здоровое растение"}
CLASS_RU = {_k(k): v for k, v in _RU.items()}
def ru_name(n): return CLASS_RU.get(_k(n))

def known_crops(model_name):
    """Культуры, которые знает модель (по русским названиям классов)."""
    try:
        return sorted({ru_name(n).split(":")[0] for n in get_model(model_name).names.values() if ru_name(n)})
    except Exception:
        return []
def _label(model, cls): return SEG_LABEL if (model == "seg" and SEG_LABEL and cls.startswith("Class ")) else cls

# ---------- изображения ----------
def exif_gps(img):
    try:
        g = img.getexif().get_ifd(0x8825)
        if not g or 2 not in g or 4 not in g: return None
        def deg(v, ref):
            d = float(v[0]) + float(v[1]) / 60 + float(v[2]) / 3600
            return -d if ref in ("S", "W") else d
        out = {"lat": round(deg(g[2], g.get(1, "N")), 6), "lon": round(deg(g[4], g.get(3, "E")), 6)}
        if 6 in g: out["alt_m"] = round(float(g[6]), 1)
        return out
    except Exception:
        return None

def load_image(data: bytes):
    """JPEG/PNG с любого устройства -> (RGB-картинка с учётом поворота EXIF, GPS или None)."""
    if len(data) > MAX_IMAGE_MB * 2**20: raise ValueError(f"Файл больше {MAX_IMAGE_MB} МБ")
    try:
        img = Image.open(io.BytesIO(data)); fmt = img.format
        if fmt not in ("JPEG", "PNG"): raise ValueError(f"Формат {fmt} не поддерживается: нужен JPEG или PNG")
        gps = exif_gps(img)
        return ImageOps.exif_transpose(img).convert("RGB"), gps
    except ValueError:
        raise
    except Exception:
        raise ValueError("Не удалось прочитать изображение (нужен JPEG или PNG)")

def _windows(W, H, tile, overlap):
    step = max(1, int(tile * (1 - overlap)))
    def axis(n):
        v = list(range(0, max(n - tile, 0) + 1, step))
        if v[-1] + tile < n: v.append(n - tile)
        return v
    return [(x, y) for y in axis(H) for x in axis(W)]

def _dets(r, model, w, h):
    names, out = r.names, []
    polys = r.masks.xy if getattr(r, "masks", None) is not None else None
    for i, b in enumerate(r.boxes):
        c = _label(model, names[int(b.cls)])
        d = {"class": c, "confidence": round(float(b.conf), 3), "bbox": [round(x, 1) for x in b.xyxy[0].tolist()]}
        if polys is not None and i < len(polys) and len(polys[i]) > 2:
            x, y = polys[i][:, 0], polys[i][:, 1]
            d["area_pct"] = round(float(0.5 * abs((x * np.roll(y, 1)).sum() - (y * np.roll(x, 1)).sum())) / (w * h) * 100, 3)
        if model == "disease": d["disease"] = is_disease(c)
        out.append(d)
    return sorted(out, key=lambda d: -d["confidence"])

def _tiled(m, img, model, conf, iou, tile, overlap, imgsz):
    import torch, torchvision
    W, H = img.size; boxes, scores, cls, names = [], [], [], m.names
    for x, y in _windows(W, H, tile, overlap):
        r = m.predict(img.crop((x, y, min(x + tile, W), min(y + tile, H))), conf=conf, iou=iou, imgsz=imgsz, verbose=False)[0]
        for b in r.boxes:
            x1, y1, x2, y2 = b.xyxy[0].tolist()
            boxes.append([x1 + x, y1 + y, x2 + x, y2 + y]); scores.append(float(b.conf)); cls.append(int(b.cls))
    if not boxes: return []
    keep = torchvision.ops.batched_nms(torch.tensor(boxes), torch.tensor(scores), torch.tensor(cls), iou).tolist()
    out = []
    for k in keep:
        c = _label(model, names[cls[k]])
        d = {"class": c, "confidence": round(scores[k], 3), "bbox": [round(v, 1) for v in boxes[k]]}
        if model == "disease": d["disease"] = is_disease(c)
        out.append(d)
    return sorted(out, key=lambda d: -d["confidence"])

def _draw(img, dets):
    im = img.copy(); dr = ImageDraw.Draw(im); wd = max(2, im.width // 500)
    try: font = ImageFont.load_default(size=max(12, im.width // 90))
    except Exception: font = None
    for d in dets:
        col = (200, 67, 47) if d.get("disease") else (63, 143, 79)
        dr.rectangle(d["bbox"], outline=col, width=wd)
        dr.text((d["bbox"][0] + 3, d["bbox"][1] + 3), f'{d["class"]} {d["confidence"]:.2f}', fill=col, font=font)
    return im

def _b64(im, side=1600):
    im = im.copy(); im.thumbnail((side, side)); buf = io.BytesIO(); im.save(buf, "JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode()

def detect_image(img, model="disease", conf=None, iou=0.5, augment=False, annotate=False, tile=False, imgsz=640):
    m = get_model(model); conf = DEFAULT_CONF[model] if conf is None else conf
    W, H = img.size; tiled = bool(tile and max(W, H) > 1000); ann = None
    with infer_lock:
        if tiled:
            dets = _tiled(m, img, model, conf, iou, 640, 0.2, imgsz)
            if annotate: ann = _draw(img, dets)
        else:
            r = m.predict(img, conf=conf, iou=iou, imgsz=imgsz, augment=augment, verbose=False)[0]
            dets = _dets(r, model, W, H)
            if annotate: ann = Image.fromarray(r.plot()[..., ::-1])
    counts = {}
    for d in dets:
        counts[d["class"]] = counts.get(d["class"], 0) + 1
        if ru_name(d["class"]): d["class_ru"] = ru_name(d["class"])
    out = {"model": model, "image": {"width": W, "height": H}, "tiled": tiled, "conf": conf, "detections": dets,
           "diseased": [d for d in dets if d.get("disease")], "counts": counts}
    if model == "seg": out["warning"] = SEG_WARNING
    if ann is not None: out["annotated_jpeg_base64"] = _b64(ann)
    return out

# ---------- видео ----------
def start_video_job(path, model, conf, iou, stride, max_seconds, track, save_video, imgsz):
    now = time.time()
    for k in [k for k, j in jobs.items() if now - j["created"] > 6 * 3600]:   # уборка старых задач и файлов
        jobs.pop(k, None)
        try: os.remove(os.path.join(VIDEO_DIR, f"{k}.mp4"))
        except OSError: pass
    jid = uuid.uuid4().hex[:12]
    jobs[jid] = {"state": "queued", "progress": 0.0, "created": now, "model": model}
    threading.Thread(target=_video_worker, args=(jid, path, model, DEFAULT_CONF[model] if conf is None else conf,
                     iou, max(1, stride), max_seconds, track, save_video, imgsz), daemon=True).start()
    return jid

def _video_worker(jid, path, model, conf, iou, stride, max_seconds, track, save_video, imgsz):
    j = jobs[jid]
    try:
        import cv2
        with video_sem:
            j["state"] = "running"
            m = get_model(model, "video")   # отдельный экземпляр: у трекера своё состояние
            cap = cv2.VideoCapture(path)
            if not cap.isOpened(): raise RuntimeError("Не удалось открыть видео (подойдут MP4, AVI, MOV)")
            fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
            total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
            limit = int(max_seconds * fps) if max_seconds else 0
            todo = min(total, limit) if (total and limit) else (total or limit)
            writer, out_path = None, os.path.join(VIDEO_DIR, f"{jid}.mp4")
            ids, timeline, keys, analyzed, idx = set(), [], [], 0, 0
            while not (limit and idx >= limit):
                if not cap.grab(): break
                if idx % stride == 0:
                    ok, frame = cap.retrieve()
                    if ok:
                        h, w = frame.shape[:2]
                        if w > 1280: frame = cv2.resize(frame, (1280, int(h * 1280 / w)))
                        with infer_lock:
                            r = (m.track(frame, persist=True, conf=conf, iou=iou, imgsz=imgsz, verbose=False, tracker="bytetrack.yaml")
                                 if track else m.predict(frame, conf=conf, iou=iou, imgsz=imgsz, verbose=False))[0]
                        n = len(r.boxes); analyzed += 1
                        if track and r.boxes.id is not None: ids.update(r.boxes.id.int().tolist())
                        timeline.append({"t": round(idx / fps, 2), "count": n})
                        need_img = save_video or (n > 0 and (len(keys) < 3 or n > min(k["count"] for k in keys)))
                        plot = r.plot() if need_img else None
                        if save_video:
                            if writer is None:
                                writer = cv2.VideoWriter(out_path, cv2.VideoWriter_fourcc(*"mp4v"), max(fps / stride, 1.0), (plot.shape[1], plot.shape[0]))
                            writer.write(plot)
                        if n > 0 and plot is not None and (len(keys) < 3 or n > min(k["count"] for k in keys)):
                            keys.append({"t": round(idx / fps, 2), "count": n, "jpeg_base64": _b64(Image.fromarray(plot[..., ::-1]), 960)})
                            keys = sorted(keys, key=lambda k: -k["count"])[:3]
                idx += 1
                if todo: j["progress"] = round(min(idx / todo, 0.99), 3)
            cap.release()
            if writer: writer.release()
            counts = [t["count"] for t in timeline]
            step = max(1, len(timeline) // 300)
            j["result"] = {"model": model, "duration_s": round(idx / fps, 1), "fps": round(fps, 1), "stride": stride,
                           "frames_analyzed": analyzed, "unique_objects": len(ids) if track else None,
                           "max_per_frame": max(counts) if counts else 0, "avg_per_frame": round(sum(counts) / len(counts), 2) if counts else 0,
                           "timeline": timeline[::step], "keyframes": keys,
                           "video_ready": bool(save_video and os.path.exists(out_path))}
            if model == "seg": j["result"]["warning"] = SEG_WARNING
            j.update(state="done", progress=1.0)
    except Exception as e:
        j.update(state="error", error=f"{type(e).__name__}: {e}")
    finally:
        try: os.remove(path)
        except OSError: pass
