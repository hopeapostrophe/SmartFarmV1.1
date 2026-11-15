"""Типы почвы (классификация USDA по песку и глине) и справочник культур. Значения ориентировочные: сверить с агрономом."""

def usda_texture(sand, clay):
    silt = 100 - sand - clay
    if silt + 1.5 * clay < 15: return "sandy"
    if silt + 1.5 * clay >= 15 and silt + 2 * clay < 30: return "loamy_sand"
    if (7 <= clay < 20 and sand > 52 and silt + 2 * clay >= 30) or (clay < 7 and silt < 50 and silt + 2 * clay >= 30): return "sandy_loam"
    if 7 <= clay < 27 and 28 <= silt < 50 and sand <= 52: return "loam"
    if (silt >= 50 and 12 <= clay < 27) or (50 <= silt < 80 and clay < 12): return "silt_loam"
    if silt >= 80 and clay < 12: return "silt"
    if 20 <= clay < 35 and silt < 28 and sand > 45: return "sandy_clay_loam"
    if 27 <= clay < 40 and 20 < sand <= 45: return "clay_loam"
    if 27 <= clay < 40 and sand <= 20: return "silty_clay_loam"
    if clay >= 35 and sand > 45: return "sandy_clay"
    if clay >= 40 and silt >= 40: return "silty_clay"
    if clay >= 40 and sand <= 45 and silt < 40: return "clay"
    return "loam"

# (точка увядания, полевая влагоёмкость), м3/м3
HYDRO = {"sandy": (0.06, 0.15), "loamy_sand": (0.07, 0.17), "sandy_loam": (0.08, 0.22), "loam": (0.12, 0.30),
         "silt_loam": (0.13, 0.32), "silt": (0.10, 0.30), "sandy_clay_loam": (0.17, 0.28), "clay_loam": (0.20, 0.35),
         "silty_clay_loam": (0.22, 0.38), "sandy_clay": (0.22, 0.33), "silty_clay": (0.25, 0.40), "clay": (0.25, 0.42)}
# пористость: доля насыщения (NASA POWER) -> м3/м3
POROSITY = {"sandy": 0.40, "loamy_sand": 0.41, "sandy_loam": 0.43, "loam": 0.45, "silt_loam": 0.46, "silt": 0.46,
            "sandy_clay_loam": 0.42, "clay_loam": 0.47, "silty_clay_loam": 0.48, "sandy_clay": 0.43, "silty_clay": 0.49, "clay": 0.50}

# культура: (глубина корней м, засухоустойчивость 1-5, бобовая)
CROPS = {
    "wheat": (0.9, 3, False), "barley": (0.8, 3, False), "corn": (1.2, 2, False), "sorghum": (1.5, 5, False),
    "millet": (1.0, 5, False), "chickpea": (1.0, 4, True), "soybean": (0.9, 2, True), "sunflower": (1.5, 4, False),
    "potato": (0.5, 1, False), "alfalfa": (2.0, 4, True), "lentil": (0.8, 4, True),
    "rice": (0.6, 1, False), "sugar_beet": (1.2, 3, False), "beetroot": (0.7, 2, False), "cotton": (1.3, 4, False),
    "rapeseed": (1.0, 3, False), "tomato": (0.8, 2, False), "pepper": (0.6, 2, False), "eggplant": (0.7, 2, False),
    "cucumber": (0.6, 1, False), "pumpkin": (1.2, 3, False), "watermelon": (1.2, 3, False), "melon": (1.0, 3, False),
    "cabbage": (0.5, 1, False), "carrot": (0.6, 2, False), "onion": (0.4, 1, False), "garlic": (0.4, 1, False)}
