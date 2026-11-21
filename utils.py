import math
import io
import base64
from matplotlib.figure import Figure


def get_distance(lat1, lon1, lat2, lon2):
    """
    Расстояние между двумя точками на Земле по формуле гаверсинусов.
    Возвращает результат в километрах. Используется, чтобы проверить,
    что запасной аэродром не оказался на другом конце света.
    """
    R = 6371.0  # средний радиус Земли, км
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlon / 2) ** 2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


def calculate_ema(data, window=3):
    """
    Экспоненциальное скользящее среднее (EMA).
    Сглаживает «прыгающий» график температуры, чтобы было видно общий тренд,
    а не каждый случайный всплеск. Чем больше window — тем плавнее линия.
    Формула: EMA_new = P * alpha + EMA_prev * (1 - alpha), где alpha = 2 / (1 + N).
    """
    if not data:
        return []
    alpha = 2 / (1 + window)
    ema = [data[0]]
    for p in data[1:]:
        current_ema = (p * alpha) + (ema[-1] * (1 - alpha))
        ema.append(current_ema)
    return ema


def safe_float(val):
    """
    Безопасный перевод в float. Если API вернул None, пустую строку или текст
    вроде «M05» (минус 5 в METAR), просто возвращаем None, а не роняем сервер.
    """
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def parse_metar_item(m):
    """
    Разбор сырого JSON-ответа от aviationweather.gov.
    Переводит все величины в систему СИ:
      - ветер из узлов в м/с (1 узел = 0.514 м/с),
      - высоту облаков из футов в метры (1 фут = 0.3048 м),
      - видимость из статутных миль в метры (1 миля = 1609.34 м),
      - давление из дюймов рт. ст. в гПа (если значение < 100).
    Возвращает кортеж для прямой подстановки в SQL INSERT.
    """
    temp = safe_float(m.get("temp"))
    dewp = safe_float(m.get("dewp"))

    # Ветер: узлы → м/с
    wspd_raw = safe_float(m.get("wspd"))
    wspd = round(wspd_raw * 0.514, 1) if wspd_raw is not None else None

    # Направление ветра: если VRB (переменный) — пишем None
    wdir = m.get("wdir")
    wdir = int(wdir) if (wdir != "VRB" and str(wdir).isdigit()) else None

    # Видимость: API отдаёт либо мили, либо метры, либо «CAVOK»/«10+»
    vis_raw = m.get("visib", "0")
    if isinstance(vis_raw, str) and ('+' in vis_raw or 'CAVOK' in vis_raw):
        vis = 9999  # неограниченная видимость
    else:
        val = safe_float(vis_raw)
        if val is None:
            vis = 0
        else:
            # Если число маленькое (< 50) — это точно мили, иначе уже метры
            vis = int(val * 1609.34) if val < 50 else int(val)

    # Нижняя граница облачности: футы → метры
    clouds = m.get("clouds", [])
    cbase_raw = safe_float(clouds[0].get("base")) if clouds else None
    cbase = int(cbase_raw * 0.3048) if cbase_raw is not None else None

    # Давление: гПа или дюймы рт. ст.
    press_raw = safe_float(m.get("altim"))
    if press_raw is not None:
        press = int(press_raw) if press_raw > 100 else int(press_raw * 33.8639)
    else:
        press = None

    return (temp, dewp, wdir, wspd, vis, cbase, press, m.get("rawOb", ""))


def validate_route(selected, coords):
    """
    Проверка маршрута на здравый смысл:
      1. Запасной аэродром не должен быть дальше 2000 км от аэродрома прилёта.
      2. Запасной не должен быть ближе к вылету, чем сам маршрут
         (иначе мы фактически летим назад, а не в обход).
    Возвращает (True, "") если всё ок, или (False, "текст ошибки").
    """
    if len(coords) != len(selected):
        return False, "Ошибка: в базе отсутствуют координаты для одного из аэродромов"

    dep_lat, dep_lon = coords[selected[0]]
    arr_lat, arr_lon = coords[selected[1]]
    dist_dep_arr = get_distance(dep_lat, dep_lon, arr_lat, arr_lon)

    for alt_icao in selected[2:]:
        alt_lat, alt_lon = coords[alt_icao]
        if get_distance(arr_lat, arr_lon, alt_lat, alt_lon) > 2000:
            return False, "Ошибка: Запасной слишком далеко от аэропорта прилёта"
        if get_distance(dep_lat, dep_lon, alt_lat, alt_lon) < dist_dep_arr - 1000:
            return False, "Ошибка: Запасной аэродром находится не по маршруту"
    return True, ""


def check_weather_for_icao(cursor, icao, ac_cat):
    """
    Единая функция проверки погоды для одного аэродрома.
    Берёт из БД последний METAR и минимумы для выбранной категории ВС,
    сравнивает видимость и облачность и возвращает готовый dict для шаблона.
    Вынесено из app.py, чтобы маршрут /check не раздувался до 100 строк.
    """
    cursor.execute("""
        SELECT m.visibility, m.cloud_base FROM measurements m
        JOIN aerodromes a ON m.aerodrome_id = a.id
        WHERE a.icao = %s ORDER BY m.timestamp DESC LIMIT 1
    """, (icao,))
    weather = cursor.fetchone()

    cursor.execute("""
        SELECT min_visibility, min_cloud_base, category FROM minima mi
        JOIN aerodromes a ON mi.aerodrome_id = a.id
        WHERE a.icao = %s AND mi.aircraft_category = %s LIMIT 1
    """, (icao, ac_cat))
    minima = cursor.fetchone()

    if weather and minima:
        vis_raw, cloud_raw = weather
        min_vis_raw, min_cloud_raw, cat = minima
        vis = vis_raw if vis_raw is not None else 0
        cloud = cloud_raw if cloud_raw is not None else 9999
        min_vis = min_vis_raw if min_vis_raw is not None else 0
        min_cloud = min_cloud_raw if min_cloud_raw is not None else 9999
        is_flyable = (vis >= min_vis and cloud >= min_cloud)
        return {
            "icao": icao, "vis": vis, "min_vis": min_vis,
            "cloud": cloud, "min_cloud": min_cloud, "cat": cat,
            "status": "ЛЁТНО" if is_flyable else "НЕЛЁТНО",
            "color": "green" if is_flyable else "red"
        }
    else:
        return {
            "icao": icao, "vis": "-", "min_vis": "-",
            "cloud": "-", "min_cloud": "-",
            "status": "НЕТ ДАННЫХ", "color": "gray"
        }


def build_weather_chart(valid_history, selected_icao, metric, window_size):
    """
    Строит график истории погоды и кодирует PNG в base64,
    чтобы вставить картинку прямо в HTML без сохранения на диск.

    ВАЖНО: используем Figure() напрямую, а не plt.subplots().
    Это нужно для потокобезопасности: если два пользователя одновременно
    запросят график, matplotlib не перепутает их фигуры в глобальном состоянии.
    """
    times = [str(h[0])[11:16] for h in valid_history]

    fig = Figure(figsize=(9, 4))
    ax = fig.subplots()
    ax.tick_params(axis='x', rotation=45)

    # Словарь настроек для каждого параметра: индекс в кортеже, подпись, цвет, маркер
    metrics_cfg = {
        'temp':  (1, 'Температура (C)', 'red',    'o', 'Температура', 'C'),
        'wind':  (2, 'Ветер (м/с)',     'green',  's', 'Ветер',       'м/с'),
        'vis':   (3, 'Видимость (м)',   'blue',   '^', 'Видимость',   'м'),
        'press': (4, 'Давление (гПа)',  'purple', 'd', 'Давление',    'гПа'),
    }
    idx, label, color, marker, title, unit = metrics_cfg.get(metric, metrics_cfg['temp'])

    values = [float(h[idx]) if h[idx] is not None else 0.0 for h in valid_history]
    ax.plot(times, values, marker=marker, color=color, label=label)

    # Для температуры накладываем сверху сглаженную EMA-кривую
    if metric == 'temp':
        ema_values = calculate_ema(values, window=window_size)
        ax.plot(times, ema_values, color='black', linewidth=2,
                label=f'Сглаживание EMA (окно {window_size})')
        ax.legend()

    ax.set_title(f'{title} на аэродроме {selected_icao}')
    ax.set_ylabel(f'{title} ({unit})')
    ax.grid(True, linestyle='--', alpha=0.6)
    fig.tight_layout()

    # Сохраняем в память (BytesIO), а не на диск — экономим место и IO
    img = io.BytesIO()
    fig.savefig(img, format='png', bbox_inches='tight')
    img.seek(0)
    chart_url = base64.b64encode(img.getvalue()).decode()

    # Считаем простую статистику для таблицы под графиком
    stats = [{
        "name": title,
        "unit": unit,
        "min": round(min(values), 1),
        "max": round(max(values), 1),
        "avg": round(sum(values) / len(values), 1)
    }]
    return chart_url, stats
