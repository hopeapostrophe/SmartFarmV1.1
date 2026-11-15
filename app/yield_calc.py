"""Калькулятор урожая (макро/микро импакт). Значения для новых культур ориентировочные — сверить с агрономом."""
from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/api/v1/yield", tags=["yield"])
L2 = 5.0  # потери с системой, %
DEFAULT_CUSTOM_L1 = 15.0

# (ru, en, kk, базовая урожайность т/га, прирост %)
CROPS = [
    ("Пшеница", "Wheat", "Бидай", 4.0, 15), ("Томаты", "Tomato", "Қызанақ", 37.5, 20),
    ("Кукуруза", "Corn", "Жүгері", 6.4, 15), ("Картофель", "Potato", "Картоп", 20.0, 25),
    ("Морковь", "Carrot", "Сәбіз", 30.0, 20), ("Огурцы", "Cucumber", "Қияр", 25.0, 30),
    ("Лук", "Onion", "Пияз", 25.0, 20), ("Подсолнечник", "Sunflower", "Күнбағыс", 2.0, 15),
    ("Арбуз", "Watermelon", "Қарбыз", 30.0, 25), ("Капуста", "Cabbage", "Қырыққабат", 40.0, 20),
    ("Рис", "Rice", "Күріш", 6.0, 15), ("Ячмень", "Barley", "Арпа", 3.5, 15),
    ("Сахарная свёкла", "Sugar beet", "Қант қызылшасы", 50.0, 20), ("Дыня", "Melon", "Қауын", 25.0, 25),
    ("Перец", "Pepper", "Бұрыш", 30.0, 20), ("Баклажан", "Eggplant", "Баклажан", 35.0, 20),
    ("Тыква", "Pumpkin", "Асқабақ", 25.0, 20), ("Чеснок", "Garlic", "Сарымсақ", 10.0, 20),
    ("Хлопок", "Cotton", "Мақта", 3.0, 15), ("Соя", "Soybean", "Соя", 2.5, 15),
    ("Рапс", "Rapeseed", "Рапс", 2.5, 15), ("Свёкла", "Beetroot", "Қызылша", 40.0, 20),
]

def default_l1(growth_pct: float) -> float:
    return (1 - (1 - L2 / 100) / (1 + growth_pct / 100)) * 100

def calculate(area_ha: float, yield_now: float, l1: float) -> dict:
    if l1 <= L2:
        return {"gain": False, "message": f"Потери без системы ({l1:.1f}%) не больше потерь с системой ({L2}%): прироста не будет."}
    h = yield_now / (1 - l1 / 100)  # потенциальная урожайность
    before = area_ha * h * (1 - l1 / 100)
    after = area_ha * h * (1 - L2 / 100)
    return {"gain": True, "l1_pct": round(l1, 1), "l2_pct": L2,
            "yield_before_t_ha": round(before / area_ha, 2), "yield_after_t_ha": round(after / area_ha, 2),
            "total_before_t": round(before, 2), "total_after_t": round(after, 2),
            "gain_t": round(after - before, 2), "gain_pct": round((after / before - 1) * 100, 1),
            "y_saved_t": round(area_ha * h * (l1 - L2) / 100, 2)}

@router.get("/crops")
def crops():
    return [{"id": i, "ru": c[0], "en": c[1], "kk": c[2], "base_yield_t_ha": c[3],
             "default_l1_pct": round(default_l1(c[4]), 1)} for i, c in enumerate(CROPS, 1)]

@router.get("/impact")
def impact(area_ha: float, crop_id: int = 0, current_yield: float | None = None,
           l1: float | None = None, custom_name: str = "Custom"):
    """crop_id 1..22 — из списка; 0 — своя культура (тогда обязателен current_yield)."""
    if area_ha <= 0: raise HTTPException(422, "area_ha должен быть > 0")
    if crop_id == 0:
        if not current_yield or current_yield <= 0: raise HTTPException(422, "Для своей культуры укажите current_yield")
        name, y = custom_name, current_yield
        l1 = DEFAULT_CUSTOM_L1 if l1 is None else l1
    elif 1 <= crop_id <= len(CROPS):
        c = CROPS[crop_id - 1]
        name, y = {"ru": c[0], "en": c[1], "kk": c[2]}, current_yield or c[3]
        l1 = default_l1(c[4]) if l1 is None else l1
    else:
        raise HTTPException(404, "Нет такой культуры, см. /api/v1/yield/crops")
    if not 0 <= l1 < 100: raise HTTPException(422, "l1 должен быть в диапазоне 0..100")
    return {"crop": name, "area_ha": area_ha, "current_yield_t_ha": y, **calculate(area_ha, y, l1)}
