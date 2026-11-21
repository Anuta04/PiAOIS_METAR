import psycopg2
import config


def get_db_connection():
    """
    Создаёт и возвращает соединение с PostgreSQL.
    Параметры подключения берутся из config.py — так мы изолируем
    «грязные» учётные данные от остального кода.
    """
    return psycopg2.connect(
        host=config.DB_HOST,
        database=config.DB_NAME,
        user=config.DB_USER,
        password=config.DB_PASS,
        port=config.DB_PORT
    )
