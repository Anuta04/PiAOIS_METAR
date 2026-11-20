# ============================================================
# ШАБЛОН КОНФИГУРАЦИИ
# Скопируйте этот файл в config.py и заполните своими данными:
#   cp config.example.py config.py
# Файл config.py НЕ должен попасть в Git (он в .gitignore)!
# ============================================================

# --- Настройки подключения к PostgreSQL ---
DB_HOST = "localhost"          # Адрес сервера БД (например, localhost или 192.168.1.10)
DB_NAME = "metar_db"           # Имя базы данных
DB_USER = "postgres"           # Имя пользователя БД
DB_PASS = "your_db_password"   # Пароль от БД (замените на свой)
DB_PORT = "5432"               # Порт PostgreSQL (по умолчанию 5432)

# --- Секретный ключ Flask (для подписи сессий и cookie) ---
# Сгенерировать можно так: python -c "import secrets; print(secrets.token_hex(32))"
SECRET_KEY = "your_secret_key"