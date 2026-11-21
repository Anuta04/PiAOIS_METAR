import time
import threading
from contextlib import contextmanager
from functools import wraps
import requests
from flask import Flask, render_template, request, session, redirect, url_for
import config
import database
import utils

# Создаём экземпляр Flask-приложения.
# SECRET_KEY нужен для подписи сессионных cookie — без него сессии не работают.
app = Flask(__name__)
app.secret_key = config.SECRET_KEY


#  ВСПОМОГАТЕЛЬНЫЕ ИНСТРУМЕНТЫ 

@contextmanager
def get_db():
    """
    Контекстный менеджер для работы с БД.
    Сам открывает соединение и курсор, делает commit при успехе,
    откатывает транзакцию при ошибке и гарантированно закрывает всё в finally.
    Использование:
        with get_db() as cursor:
            cursor.execute("SELECT ...")
    """
    conn = database.get_db_connection()
    cursor = conn.cursor()
    try:
        yield cursor
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()


def login_required(f):
    """
    Декоратор-страж: если пользователь не залогинен (нет user_id в сессии),
    молча редиректим его на страницу входа.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    """
    Декоратор для админки: пускает только тех, у кого role == 'admin'.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        if session.get('role') != 'admin':
            return "Доступ запрещен."
        return f(*args, **kwargs)
    return decorated


#  ФОНОВЫЙ СБОР METAR 

def background_collector():
    """
    Фоновый поток-сборщик METAR. Запускается один раз при старте Flask
    и крутится в бесконечном цикле с паузой в 1 час между итерациями.

    Логика работы:
      1. Берём из БД список всех ICAO аэродромов.
      2. Для каждого проверяем: а не писали ли мы уже METAR в текущий час?
         (защита от дублей — чтобы при перезапуске приложения не задвоить данные).
      3. Если записи ещё нет — идём в NOAA API, парсим JSON и кладём в БД.
      4. После прохода по всем аэродромам засыпаем на 3600 секунд.
    """
    time.sleep(5)  # Даём Flask время подняться, чтобы не блокировать старт
    url = "https://aviationweather.gov/api/data/metar"

    while True:
        print("\n[ФОН] Начинаю сбор METAR")
        try:
            with get_db() as cursor:
                cursor.execute("SELECT icao FROM aerodromes")
                airports = [icao for (icao,) in cursor.fetchall()]

            success = 0
            for icao in airports:
                try:
                    # Защита от дублей: если за текущий час уже есть запись — пропускаем
                    with get_db() as cursor:
                        cursor.execute("""
                            SELECT COUNT(*) FROM measurements
                            WHERE aerodrome_id = (SELECT id FROM aerodromes WHERE icao = %s)
                              AND timestamp >= date_trunc('hour', NOW())
                        """, (icao,))
                        (existing_count,) = cursor.fetchone()
                        if existing_count > 0:
                            continue

                    # Запрос к внешнему API
                    response = requests.get(
                        url, params={"ids": icao, "format": "json"}, timeout=10
                    )
                    data = response.json()
                    if not data:
                        continue

                    first_item, *_ = data
                    parsed = utils.parse_metar_item(first_item)

                    # Сохраняем распарсенные данные в БД
                    with get_db() as cursor:
                        cursor.execute("""
                            INSERT INTO measurements (
                                aerodrome_id, timestamp, temperature, dew_point,
                                wind_direction, wind_speed, visibility, cloud_base,
                                pressure, metar_raw
                            )
                            VALUES (
                                (SELECT id FROM aerodromes WHERE icao = %s),
                                NOW(), %s, %s, %s, %s, %s, %s, %s, %s
                            )
                        """, (icao, *parsed))
                    success += 1
                    time.sleep(0.5)  # небольшая пауза, чтобы не долбить API
                except Exception as item_err:
                    print(f"[ФОН] Ошибка по {icao}: {item_err}")

            print(f"[ФОН] Обновлено {success} аэродромов.")
        except Exception as e:
            print(f"[ФОН] Критическая ошибка: {e}")

        time.sleep(3600)  # пауза между итерациями — 1 час


#  АВТОРИЗАЦИЯ И РЕГИСТРАЦИЯ 

@app.route('/register', methods=['GET', 'POST'])
def register():
    """Регистрация нового пользователя."""
    error = None
    if request.method == 'POST':
        login_input = request.form.get('username')
        password_input = request.form.get('password')
        with get_db() as cursor:
            cursor.execute("SELECT id FROM users WHERE login = %s", (login_input,))
            if cursor.fetchone():
                error = "Пользователь с таким логином уже существует"
            else:
                cursor.execute(
                    "INSERT INTO users (login, password_hash, role, active) "
                    "VALUES (%s, %s, 'user', true)",
                    (login_input, password_input)
                )
        return redirect(url_for('login'))
    return render_template('register.html', error=error)


@app.route('/login', methods=['GET', 'POST'])
def login():
    """Авторизация: сверяем логин+пароль с БД и кладём данные юзера в сессию Flask."""
    error = None
    if request.method == 'POST':
        login_input = request.form.get('username')
        password_input = request.form.get('password')
        with get_db() as cursor:
            cursor.execute(
                "SELECT id, login, role FROM users "
                "WHERE login = %s AND password_hash = %s",
                (login_input, password_input)
            )
            user = cursor.fetchone()
        if user:
            user_id, username, role = user
            session['user_id'] = user_id
            session['username'] = username
            session['role'] = role
            return redirect(url_for('dashboard'))
        error = "Неверный логин или пароль"
    return render_template('login.html', error=error)


@app.route('/logout')
def logout():
    """Выход: чистим сессию и редиректим на логин."""
    session.clear()
    return redirect(url_for('login'))


#  ОСНОВНЫЕ СТРАНИЦЫ 

@app.route('/')
@app.route('/dashboard')
@login_required
def dashboard():
    """Главное меню после входа — просто стартовая страница."""
    return render_template('dashboard.html')


@app.route('/check', methods=['GET', 'POST'])
@login_required
def check():
    """
    Предполётная проверка: пользователь выбирает вылет/прилёт/запасные и категорию ВС,
    а мы для каждого аэродрома сравниваем свежий METAR с метеоминимумами
    и выдаём вердикт «ЛЁТНО / НЕЛЁТНО».
    """
    results = []
    with get_db() as cursor:
        cursor.execute(
            "SELECT icao, COALESCE(name_ru, name), city FROM aerodromes "
            "ORDER BY COALESCE(name_ru, name)"
        )
        aerodromes = cursor.fetchall()

        if request.method == 'POST':
            selected = [request.form.get(k) for k in ('dep', 'arr', 'alt1', 'alt2')]
            selected = [s for s in selected if s]
            ac_cat = request.form.get('ac_cat', 'C')

            # Простые проверки валидации формы
            if len(selected) != len(set(selected)):
                results.append({"icao": "ОШИБКА",
                                "status": "Ошибка: аэродромы не должны повторяться",
                                "color": "red"})
            elif len(selected) < 3:
                results.append({"icao": "ОШИБКА",
                                "status": "Выберите вылет, прилёт и хотя бы один запасной",
                                "color": "gray"})
            else:
                # Собираем координаты для проверки маршрута
                coords = {}
                for icao in selected:
                    cursor.execute(
                        "SELECT latitude, longitude FROM aerodromes WHERE icao = %s",
                        (icao,)
                    )
                    row = cursor.fetchone()
                    if row and row[0] is not None and row[1] is not None:
                        coords[icao] = (float(row[0]), float(row[1]))

                is_valid, error_msg = utils.validate_route(selected, coords)
                if not is_valid:
                    results.append({
                        "icao": "МАРШРУТ", "vis": "-", "min_vis": "-",
                        "cloud": "-", "min_cloud": "-", "cat": "-",
                        "status": error_msg, "color": "red"
                    })
                else:
                    # Для каждого аэродрома — отдельная проверка через utils
                    for icao in selected:
                        results.append(utils.check_weather_for_icao(cursor, icao, ac_cat))

    return render_template('check.html', aerodromes=aerodromes,
                           results=results, form_data=request.form)


@app.route('/history', methods=['GET', 'POST'])
@login_required
def history():
    """
    Аналитика истории: пользователь выбирает аэродром, параметр и окно сглаживания,
    а мы строим график и считаем min/max/avg по выбранным замерам.
    """
    chart_url = None
    stats = []
    selected = ""
    limit = "10"
    metric = "temp"
    window_size = 3

    with get_db() as cursor:
        cursor.execute(
            "SELECT icao, COALESCE(name_ru, name) FROM aerodromes "
            "ORDER BY COALESCE(name_ru, name)"
        )
        aerodromes = cursor.fetchall()

        if request.method == 'POST':
            selected = request.form.get('icao')
            limit = request.form.get('limit', '10')
            metric = request.form.get('metric', 'temp')
            window_size = int(request.form.get('window', 3))
            limit_val = int(limit) if limit.isdigit() else 10

            # Берём последние N записей и разворачиваем в хронологическом порядке
            cursor.execute("""
                SELECT m.timestamp, m.temperature, m.wind_speed,
                       m.visibility, m.pressure
                FROM measurements m
                JOIN aerodromes a ON m.aerodrome_id = a.id
                WHERE a.icao = %s
                ORDER BY m.timestamp DESC
                LIMIT %s
            """, (selected, limit_val))
            history_data = list(reversed(cursor.fetchall()))

            # Отсеиваем полностью пустые строки
            valid_history = [h for h in history_data
                             if h and h.count(None) < len(h)]

            if len(valid_history) >= 2:
                chart_url, stats = utils.build_weather_chart(
                    valid_history, selected, metric, window_size
                )

    return render_template(
        'history.html', aerodromes=aerodromes, chart_url=chart_url,
        selected=selected, limit=limit, metric=metric,
        window=str(window_size), stats=stats
    )


# АДМИНКА 

@app.route('/admin')
@admin_required
def admin():
    """Список всех пользователей для администратора."""
    with get_db() as cursor:
        cursor.execute("SELECT id, login, role, created FROM users ORDER BY id")
        users_list = cursor.fetchall()
    return render_template('admin.html', users_list=users_list)


@app.route('/admin/toggle_role/<int:user_id>', methods=['POST'])
@admin_required
def toggle_role(user_id):
    """Переключение роли admin ↔ user. Себя менять нельзя."""
    if user_id == session.get('user_id'):
        return "Вы не можете изменить свою роль!"
    with get_db() as cursor:
        cursor.execute(
            "UPDATE users SET role = CASE WHEN role = 'admin' "
            "THEN 'user' ELSE 'admin' END WHERE id = %s",
            (user_id,)
        )
    return redirect(url_for('admin'))


@app.route('/admin/delete/<int:user_id>', methods=['POST'])
@admin_required
def delete_user(user_id):
    """Удаление пользователя. Себя удалять тоже нельзя."""
    if user_id == session.get('user_id'):
        return "Вы не можете удалить самого себя!"
    with get_db() as cursor:
        cursor.execute("DELETE FROM users WHERE id = %s", (user_id,))
    return redirect(url_for('admin'))


#  ТОЧКА ВХОДА 

if __name__ == '__main__':
    # Запускаем фоновый сборщик METAR как daemon-поток:
    # он умрёт вместе с основным процессом Flask при остановке сервера.
    bg_thread = threading.Thread(target=background_collector, daemon=True)
    bg_thread.start()
    # use_reloader=False нужен, чтобы при debug-перезагрузке не плодились два сборщика
    app.run(debug=True, use_reloader=False)
