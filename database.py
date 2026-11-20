import psycopg2
import config
# та же штуковина что и конфиг просто обернутый отдельной функцией 
def get_db_connection():
    return psycopg2.connect(
        host=config.DB_HOST, 
        database=config.DB_NAME, 
        user=config.DB_USER, 
        password=config.DB_PASS, 
        port=config.DB_PORT
    )