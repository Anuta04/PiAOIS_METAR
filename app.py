import time
import threading
from contextlib import contextmanager
from functools import wraps
import requests
from flask import Flask, render_template, request, session, redirect, url_for

import config
import database
import utils

app = Flask(__name__)
app.secret_key = config.SECRET_KEY


# --- ВСПОМОГАТЕЛЬНЫЕ ИНСТРУМЕНТЫ ---

@contextmanager
def get_db():
    """Контекстный менеджер для БД: сам открывает курсор, делает commit и закрывает соединение."""
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
    """Декоратор защиты страниц: если пользователь не вошел в систему, отправляем на /login."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    """Декоратор для админки: пускает только пользователей с ролью admin."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        if session.get('role') != 'admin':
            return "Доступ запрещен."
        return f(*args, **kwargs)
    return decorated


# --- ФОНОВЫЙ СБОР МЕТЕОДАННЫХ ---

def background_collector():
    """Фоновый поток: раз в час опрашивает API и складывает свежие сводки METAR в базу."""
    time.sleep(5)
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
                    with get_db() as cursor:
                        cursor.execute("""
                            SELECT COUNT(*) FROM measurements
                            WHERE aerodrome_id = (SELECT id FROM aerodromes WHERE icao = %s)
                              AND timestamp >= date_trunc('hour', NOW())
                        """, (icao,))
                        (existing_count,) = cursor.fetchone()
                        if existing_count > 0:
                            continue

                    response = requests.get(url, params={"ids": icao, "format": "json"}, timeout=10)
                    data = response.json()
                    if not data:
                        continue

                    first_item, *_ = data
                    parsed = utils.parse_metar_item(first_item)
                    with get_db() as cursor:
                        cursor.execute("""
                            INSERT INTO measurements (
                                aerodrome_id, timestamp, temperature, dew_point,
                                wind_direction, wind_speed, visibility, cloud_base, pressure, metar_raw
                            )
                            VALUES ((SELECT id FROM aerodromes WHERE icao = %s), NOW(), %s, %s, %s, %s, %s, %s, %s, %s)
                        """, (icao, *parsed))

                    success += 1
                    time.sleep(0.5)
                except Exception as item_err:
                    print(f"[ФОН] Ошибка по {icao}: {item_err}")

            print(f"[ФОН] Обновлено {success} аэродромов.")
        except Exception as e:
            print(f"[ФОН] Критическая ошибка: {e}")

        time.sleep(3600)


# --- АВТОРИЗАЦИЯ И РЕГИСТРАЦИЯ ---

@app.route('/register', methods=['GET', 'POST'])
def register():
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
                    "INSERT INTO users (login, password_hash, role, active) VALUES (%s, %s, 'user', true)",
                    (login_input, password_input)
                )
                return redirect(url_for('login'))

    return render_template('register.html', error=error)


@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        login_input = request.form.get('username')
        password_input = request.form.get('password')

        with get_db() as cursor:
            cursor.execute(
                "SELECT id, login, role FROM users WHERE login = %s AND password_hash = %s",
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
    session.clear()
    return redirect(url_for('login'))


# --- ОСНОВНЫЕ СТРАНИЦЫ ---

@app.route('/')
@app.route('/dashboard')
@login_required
def dashboard():
    return render_template('dashboard.html')


@app.route('/check', methods=['GET', 'POST'])
@login_required
def check():
    """Предполетная проверка: сравнивает свежую погоду с метеоминимумами выбранного самолета."""
    results = []
    with get_db() as cursor:
        cursor.execute("SELECT icao, COALESCE(name_ru, name), city FROM aerodromes ORDER BY COALESCE(name_ru, name)")
        aerodromes = cursor.fetchall()

        if request.method == 'POST':
            selected = [request.form.get(k) for k in ('dep', 'arr', 'alt1', 'alt2')]
            selected = [s for s in selected if s]
            ac_cat = request.form.get('ac_cat', 'C')

            if len(selected) != len(set(selected)):
                results.append({"icao": "ОШИБКА", "status": "Ошибка: аэродромы не должны повторяться", "color": "red"})
            elif len(selected) < 3:
                results.append({"icao": "ОШИБКА", "status": "Выберите вылет, прилет и хотя бы один запасной", "color": "gray"})
            else:
                coords = {}
                for icao in selected:
                    cursor.execute("SELECT latitude, longitude FROM aerodromes WHERE icao = %s", (icao,))
                    row = cursor.fetchone()
                    if row:
                        lat, lon = row
                        if lat is not None and lon is not None:
                            coords[icao] = (float(lat), float(lon))

                is_valid, error_msg = utils.validate_route(selected, coords)
                if not is_valid:
                    results.append({
                        "icao": "МАРШРУТ", "vis": "-", "min_vis": "-",
                        "cloud": "-", "min_cloud": "-", "cat": "-",
                        "status": error_msg, "color": "red"
                    })
                else:
                    for icao in selected:
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
                            results.append({
                                "icao": icao, "vis": vis, "min_vis": min_vis,
                                "cloud": cloud, "min_cloud": min_cloud, "cat": cat,
                                "status": "ЛЁТНО" if is_flyable else "НЕЛЁТНО",
                                "color": "green" if is_flyable else "red"
                            })
                        else:
                            results.append({
                                "icao": icao, "vis": "-", "min_vis": "-",
                                "cloud": "-", "min_cloud": "-",
                                "status": "НЕТ ДАННЫХ", "color": "gray"
                            })

    return render_template('check.html', aerodromes=aerodromes, results=results, form_data=request.form)

@app.route('/history', methods=['GET', 'POST'])
@login_required
def history():
    chart_url = None
    stats = []
    selected = ""
    limit = "10"
    metric = "temp"
    window_size = 3

    with get_db() as cursor:
        cursor.execute("SELECT icao, COALESCE(name_ru, name) FROM aerodromes ORDER BY COALESCE(name_ru, name)")
        aerodromes = cursor.fetchall()

        if request.method == 'POST':
            selected = request.form.get('icao')
            limit = request.form.get('limit', '10')
            metric = request.form.get('metric', 'temp')
            window_size = int(request.form.get('window', 3))
            limit_val = int(limit) if limit.isdigit() else 10

            cursor.execute("""
                SELECT m.timestamp, m.temperature, m.wind_speed, m.visibility, m.pressure
                FROM measurements m
                JOIN aerodromes a ON m.aerodrome_id = a.id
                WHERE a.icao = %s ORDER BY m.timestamp DESC LIMIT %s
            """, (selected, limit_val))
            history_data = list(reversed(cursor.fetchall()))
            valid_history = [h for h in history_data if h and h.count(None) < len(h)]

            if len(valid_history) >= 2:
                chart_url, stats = utils.build_weather_chart(valid_history, selected, metric, window_size)

    # <-- ЭТА СТРОКА ДОЛЖНА БЫТЬ ЗДЕСЬ (4 пробела от края, на уровне with get_db)
    return render_template(
        'history.html', aerodromes=aerodromes, chart_url=chart_url,
        selected=selected, limit=limit, metric=metric, window=str(window_size), stats=stats
    )




@app.route('/admin')
@admin_required
def admin():
    with get_db() as cursor:
        cursor.execute("SELECT id, login, role, created FROM users ORDER BY id")
        users_list = cursor.fetchall()
    return render_template('admin.html', users_list=users_list)
@app.route('/admin/toggle_role/int:user_id', methods=['POST'])
@admin_required
def toggle_role(user_id):
    if user_id == session.get('user_id'):
        return "Вы не можете изменить свою роль!"
    with get_db() as cursor:
        cursor.execute(
"UPDATE users SET role = CASE WHEN role = 'admin' THEN 'user' ELSE 'admin' END WHERE id = %s",
(user_id,)
)
    return redirect(url_for('admin'))
@app.route('/admin/delete/int:user_id', methods=['POST'])
@admin_required
def delete_user(user_id):
    if user_id == session.get('user_id'):
        return "Вы не можете удалить самого себя!"
        with get_db() as cursor:
            cursor.execute("DELETE FROM users WHERE id = %s", (user_id,))
    return redirect(url_for('admin'))
if __name__ == '__main__':
    bg_thread = threading.Thread(target=background_collector, daemon=True)
    bg_thread.start()
app.run(debug=True, use_reloader=False)