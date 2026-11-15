#!/usr/bin/env python3
"""Склеивает несколько YOLO-датасетов (экспорт Roboflow или Ultralytics) в один с общим списком классов.

  python merge_datasets.py --out merged --map map.json ds_old ds_wheat ...

map.json (необязательно): {"rename": {"wheat_brown_rust": "Wheat_Brown_Rust"}, "drop": ["rice_hispa"]}
Имена сравниваются без учёта регистра, пробелов и подчёркиваний, поэтому одинаковые классы из разных
датасетов склеиваются сами. Изображения, где остались только удалённые классы, пропускаются,
чтобы не учить модель на ложном «фоне». Подходит и для детекции, и для сегментации."""
import argparse, json, random, re, shutil
from collections import Counter
from pathlib import Path
import yaml

IMG = {".jpg", ".jpeg", ".png"}
SPLITS = {"train": "train", "valid": "val", "val": "val", "validation": "val", "test": "test"}

def norm(n): return re.sub(r"[\s_\-]+", " ", str(n).strip()).lower()

def load_names(ds):
    f = next(iter(ds.glob("data.y*ml")), None)
    if not f: raise SystemExit(f"{ds}: нет data.yaml")
    n = yaml.safe_load(f.read_text())["names"]
    return [n[i] for i in sorted(n)] if isinstance(n, dict) else list(n)

def find_pairs(ds):
    out = []
    for sp, tgt in SPLITS.items():
        for img_dir, lbl_dir in ((ds / sp / "images", ds / sp / "labels"), (ds / "images" / sp, ds / "labels" / sp)):
            if img_dir.is_dir(): out.append((tgt, img_dir, lbl_dir))
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("datasets", nargs="+"); ap.add_argument("--out", default="merged"); ap.add_argument("--map", default="")
    ap.add_argument("--val-frac", type=float, default=0.1, help="доля в val, если у датасета нет своей валидации")
    a = ap.parse_args(); random.seed(0)
    m = json.loads(Path(a.map).read_text()) if a.map else {}
    rename = {norm(k): v for k, v in m.get("rename", {}).items()}; drop = {norm(x) for x in m.get("drop", [])}
    out = Path(a.out)
    if out.exists(): shutil.rmtree(out)
    unified, uid = [], {}                      # итоговые имена и их id
    counts = {s: Counter() for s in ("train", "val", "test")}; imgs = Counter()
    for ds in map(Path, a.datasets):
        names = load_names(ds); idmap = {}
        for i, n in enumerate(names):
            if norm(n) in drop: continue
            u = rename.get(norm(n), re.sub(r"[\s_]+", " ", str(n)).strip()); key = norm(u)
            if key not in uid: uid[key] = len(unified); unified.append(u)
            idmap[i] = uid[key]
        items = []                              # (split, img, lbl)
        for tgt, img_dir, lbl_dir in find_pairs(ds):
            for img in sorted(p for p in img_dir.iterdir() if p.suffix.lower() in IMG):
                lbl = lbl_dir / (img.stem + ".txt")
                if lbl.exists(): items.append((tgt, img, lbl))
        have = {t for t, _, _ in items}
        if "val" not in have:                   # нет валидации: берём часть train
            tr = [x for x in items if x[0] == "train"]; random.shuffle(tr)
            take = {id(x) for x in tr[:max(1, int(len(tr) * a.val_frac))]}
            items = [("val", i, l) if id(x) in take else x for x in items for (_, i, l) in [x]]
        kept = 0
        for tgt, img, lbl in items:
            lines = []
            for ln in lbl.read_text().splitlines():
                t = ln.split()
                if t and int(float(t[0])) in idmap: lines.append(" ".join([str(idmap[int(float(t[0]))])] + t[1:]))
            if not lines: continue
            stem = re.sub(r"[^\w\-.]", "_", f"{ds.name}__{img.stem}")
            for sub in ("images", "labels"): (out / sub / tgt).mkdir(parents=True, exist_ok=True)
            shutil.copy2(img, out / "images" / tgt / (stem + img.suffix.lower()))
            (out / "labels" / tgt / (stem + ".txt")).write_text("\n".join(lines) + "\n")
            for ln in lines: counts[tgt][int(ln.split()[0])] += 1
            imgs[tgt] += 1; kept += 1
        print(f"{ds.name}: взято изображений {kept}, классов {len(idmap)} из {len(names)}")
    cfg = {"path": str(out.resolve()), "train": "images/train", "val": "images/val", "names": {i: n for i, n in enumerate(unified)}}
    if imgs["test"]: cfg["test"] = "images/test"
    (out / "data.yaml").write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False))
    print(f"\nИтого: train {imgs['train']}, val {imgs['val']}, test {imgs['test']} изображений, классов {len(unified)}")
    print(f"{'класс':42}{'train':>8}{'val':>8}")
    for i, n in enumerate(unified):
        tr, va = counts["train"][i], counts["val"][i]
        print(f"{n:42}{tr:>8}{va:>8}" + ("   <- мало примеров!" if tr < 100 or va < 10 else ""))

if __name__ == "__main__":
    main()
