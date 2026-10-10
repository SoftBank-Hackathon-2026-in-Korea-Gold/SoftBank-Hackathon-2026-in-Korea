import os
import socket
import time

DB_HOST = os.environ.get("DB_HOST", "localhost")
DB_PORT = int(os.environ.get("DB_PORT", "5432"))
ATTEMPTS = 5


def connect():
    addr = socket.gethostbyname(DB_HOST)  # IPv4 address of the database host
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(2)
    try:
        sock.connect((addr, DB_PORT))
    finally:
        sock.close()


print("running migrations", flush=True)
for attempt in range(1, ATTEMPTS + 1):
    try:
        connect()
        break
    except OSError as exc:
        print(f"waiting for database at {DB_HOST}:{DB_PORT} (attempt {attempt}/{ATTEMPTS}): {exc}", flush=True)
        if attempt == ATTEMPTS:
            raise
        time.sleep(2)
print("migrations applied", flush=True)
