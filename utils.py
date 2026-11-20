import math
import io
import base64
from matplotlib.figure import Figure


def get_distance(lat1, lon1, lat2, lon2):
    """Считает расстояние между двумя точками на Земле по формуле гаверсинусов (в км)."""
    R = 6371.0  # Средний радиус Земли в километрах
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


def calculate_ema(data, window=3):
    """Экспоненциальное скользящее среднее (EMA) для сглаживания скачков на графике."""
    if not data:
        return []
    
    # Коэффициент сглаживания: чем больше окно, тем плавнее линия и меньше реакция на выбросы
    alpha = 2 / (1 + window)
    ema = [data[0]]  # За точку отсчета берем первое реальное измерение
    
    for p in data[1:]:
        current_ema = (p * alpha) + (ema[-1] * (1 - alpha))
        ema.append(current_ema)
        
    return ema


def safe_float(val):
    """Безопасный перевод в float: если API прислал текст или None, не падаем, а возвращаем None."""
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def parse_metar_item(m):
    """Разбирает сырой JSON-ответ от aviationweather.gov и переводит величины в систему СИ."""
    temp = safe_float(m.get("temp"))
    dewp = safe_float(m.get("dewp"))

    # В авиации ветер в узлах (kt) — переводим в м/с (1 узел = 0.514 м/с)
    wspd_raw = safe_float(m.get("wspd"))
    wspd = round(wspd_raw * 0.514, 1) if wspd_raw is not None else None

    # Направление ветра: если VRB (переменный) или буквы — пишем None
    wdir = m.get("wdir")
    wdir = int(wdir) if (wdir != "VRB" and str(wdir).isdigit()) else None

    # Видимость: API отдает либо в статутных милях (США/мир), либо в метрах, либо CAVOK / 10+
    vis_raw = m.get("visib", "0")
    if isinstance(vis_raw, str) and ('+' in vis_raw or 'CAVOK' in vis_raw):
        vis = 9999  # Условная неограниченная видимость (10 км и более)
    else:
        val = safe_float(vis_raw)
        if val is None:
            vis = 0
        else:
            # Если число меньше 50, это точно мили (1 миля = 1609.34 м), иначе уже метры
            vis = int(val * 1609.34) if val < 50 else int(val)

    # Нижняя граница облачности приходит в футах — переводим в метры (1 фут = 0.3048 м)
    clouds = m.get("clouds", [])
    cbase_raw = safe_float(clouds[0].get("base")) if clouds else None
    cbase = int(cbase_raw * 0.3048) if cbase_raw is not None else None

    # Давление: бывает в гПа (около 1013) или в дюймах рт. ст. (около 29.92)
    press_raw = safe_float(m.get("altim"))
    if press_raw is not None:
        press = int(press_raw) if press_raw > 100 else int(press_raw * 33.8639)
    else:
        press = None

    return (temp, dewp, wdir, wspd, vis, cbase, press, m.get("rawOb", ""))


def validate_route(selected, coords):
    """Проверяет здравый смысл маршрута: чтобы запасной не был на другом краю света."""
    if len(coords) != len(selected):
        return False, "Ошибка: в базе отсутствуют координаты для одного из выбранных аэродромов"

    dep_lat, dep_lon = coords[selected[0]]
    arr_lat, arr_lon = coords[selected[1]]
    dist_dep_arr = get_distance(dep_lat, dep_lon, arr_lat, arr_lon)

    for alt_icao in selected[2:]:
        alt_lat, alt_lon = coords[alt_icao]
        # 1. Запасной должен быть в пределах 2000 км от аэродрома назначения
        if get_distance(arr_lat, arr_lon, alt_lat, alt_lon) > 2000:
            return False, "Ошибка: Запасной слишком далеко от аэропорта прилета"
        # 2. Запасной не должен быть ближе к вылету, чем сам маршрут (иначе летим назад)
        if get_distance(dep_lat, dep_lon, alt_lat, alt_lon) < dist_dep_arr - 1000:
            return False, "Ошибка: Запасной аэродром находится не по маршруту"

    return True, ""


def build_weather_chart(valid_history, selected_icao, metric, window_size):
    """Рисует график истории погоды и кодирует картинку в base64 для вставки прямо в HTML."""
    times = [str(h[0])[11:16] for h in valid_history]
    
    # Используем Figure() напрямую вместо plt.subplots(), чтобы при одновременных
    # запросах от разных пользователей графики не смешивались в памяти
    fig = Figure(figsize=(9, 4))
    ax = fig.subplots()
    ax.tick_params(axis='x', rotation=45)

    metrics_cfg = {
        'temp':  (1, 'Температура (C)', 'red',    'o', 'Температура', 'C'),
        'wind':  (2, 'Ветер (м/с)',     'green',  's', 'Ветер',       'м/с'),
        'vis':   (3, 'Видимость (м)',   'blue',   '^', 'Видимость',   'м'),
        'press': (4, 'Давление (гПа)',  'purple', 'd', 'Давление',    'гПа'),
    }
    idx, label, color, marker, title, unit = metrics_cfg.get(metric, metrics_cfg['temp'])
    values = [float(h[idx]) if h[idx] is not None else 0.0 for h in valid_history]



    ax.plot(times, values, marker=marker, color=color, label=label)

    # Для температуры поверх реальных точек накладываем сглаженную кривую EMA
    if metric == 'temp':
        ema_values = calculate_ema(values, window=window_size)
        ax.plot(times, ema_values, color='black', linewidth=2, label=f'Сглаживание EMA (окно {window_size})')
        ax.legend()

    ax.set_title(f'{title} на аэродроме {selected_icao}')
    ax.set_ylabel(f'{title} ({unit})')
    ax.grid(True, linestyle='--', alpha=0.6)
    fig.tight_layout()

    # Сохраняем график в оперативную память (BytesIO), не засоряя диск картинками
    img = io.BytesIO()
    fig.savefig(img, format='png', bbox_inches='tight')
    img.seek(0)
    chart_url = base64.b64encode(img.getvalue()).decode()

    stats = [{
        "name": title,
        "unit": unit,
        "min": round(min(values), 1),
        "max": round(max(values), 1),
        "avg": round(sum(values) / len(values), 1)
    }]
    return chart_url, stats
