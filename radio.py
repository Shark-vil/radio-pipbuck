# radio.py
# pip install flask mutagen

import os
import re
import random
import threading
import queue
import time
import signal
import sys
from flask import Flask, Response, render_template, jsonify
from mutagen.mp3 import MP3

# ─── Настройки ────────────────────────────────────────────────
MUSIC_DIR  = os.environ.get("MUSIC_DIR", r"D:\mlpStream")
HOST       = "0.0.0.0"
PORT       = int(os.environ.get("PORT", 8000))

# Маленький чанк = гранулярная отправка = плавный поток без статтеров
# 4096 байт при 128 kbps → отправка каждые ~0.25 сек (вместо ~1 сек при 16 KB)
CHUNK      = 4096
QUEUE_SIZE = 512   # ~2 MB буфер на клиента — достаточно для YouTube-ингеста
# ──────────────────────────────────────────────────────────────

app = Flask(__name__)

current_song: dict    = {"title": "—", "bitrate": 128}
clients: list         = []
title_listeners: list = []
clients_lock          = threading.Lock()
shutdown_event        = threading.Event()


# ─── Утилиты ──────────────────────────────────────────────────

def notify_title_change() -> None:
    """SSE-уведомление о смене трека (вызывать без лока)."""
    title_data = f"data: {current_song['title']}\n\n"
    dead: list = []
    with clients_lock:
        for q in title_listeners:
            try:
                q.put_nowait(title_data)
            except queue.Full:
                dead.append(q)
        for d in dead:
            if d in title_listeners:
                title_listeners.remove(d)


def clean_title(filename: str) -> str:
    name = os.path.splitext(filename)[0]
    name = re.sub(r'^\d+\s*[-—–]\s*', '', name).strip()
    return name


def get_playlist() -> list[str]:
    try:
        files = [f for f in os.listdir(MUSIC_DIR) if f.lower().endswith('.mp3')]
        random.shuffle(files)
        return files
    except FileNotFoundError:
        print(f"[radio] Папка не найдена: {MUSIC_DIR}")
        return []


# ─── Broadcaster ──────────────────────────────────────────────

def broadcaster() -> None:
    print(f"[radio] Стрим запущен. Папка: {MUSIC_DIR}")

    while not shutdown_event.is_set():
        playlist = get_playlist()
        if not playlist:
            print("[radio] Нет MP3-файлов, жду 10 сек...")
            shutdown_event.wait(10)
            continue

        # Предзагрузка: читаем метаданные ПЕРЕД началом стрима
        # Чтобы между треками не было задержки на чтение тегов
        prepared = []
        for filename in playlist:
            filepath = os.path.join(MUSIC_DIR, filename)
            try:
                audio_info = MP3(filepath)
                bitrate = audio_info.info.bitrate
            except Exception as e:
                print(f"[radio] Не удалось прочитать {filename}: {e}")
                bitrate = 128_000
            prepared.append((filename, filepath, bitrate))

        for filename, filepath, bitrate in prepared:
            if shutdown_event.is_set():
                break

            current_song["title"]   = clean_title(filename)
            current_song["bitrate"] = bitrate // 1000
            print(f"[radio] ▶ {current_song['title']}  [{current_song['bitrate']} kbps]")
            notify_title_change()

            bytes_per_sec = bitrate / 8  # байт/с

            try:
                with open(filepath, 'rb') as f:
                    # Точный таймер — не сбрасывается между чанками
                    start_time = time.perf_counter()
                    bytes_sent = 0

                    while not shutdown_event.is_set():
                        chunk = f.read(CHUNK)
                        if not chunk:
                            # Файл закончился — минимальная пауза перед следующим треком
                            # чтобы клиенты успели получить последний чанк
                            shutdown_event.wait(0.01)
                            break

                        # Копируем список под локом, рассылаем без лока
                        with clients_lock:
                            snapshot = list(clients)

                        dead_clients: list = []
                        for q in snapshot:
                            try:
                                q.put_nowait(chunk)
                            except queue.Full:
                                dead_clients.append(q)

                        if dead_clients:
                            with clients_lock:
                                for dead_q in dead_clients:
                                    if dead_q in clients:
                                        clients.remove(dead_q)
                            print(f"[radio] Дроп медленных клиентов: {len(dead_clients)}")

                        bytes_sent += len(chunk)

                        # Точный контроль темпа через perf_counter
                        # Маленький drift (0.02 с) = минимальный джиттер
                        expected = bytes_sent / bytes_per_sec
                        elapsed  = time.perf_counter() - start_time
                        drift    = expected - elapsed
                        if drift > 0.02:
                            shutdown_event.wait(drift)

            except Exception as e:
                print(f"[radio] Ошибка при чтении {filename}: {e}")

    print("[radio] Broadcaster остановлен.")


# ─── Роуты ────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/stream')
def stream():
    client_q: queue.Queue = queue.Queue(maxsize=QUEUE_SIZE)

    with clients_lock:
        clients.append(client_q)

    print(f"[radio] + Слушатель (всего: {len(clients)})")

    def generate():
        try:
            consecutive_empty = 0
            max_consecutive_empty = 400  # ~20 сек без данных = отключение
            while not shutdown_event.is_set():
                try:
                    chunk = client_q.get(timeout=0.05)
                    consecutive_empty = 0
                    yield chunk
                except queue.Empty:
                    consecutive_empty += 1
                    if consecutive_empty >= max_consecutive_empty:
                        print("[radio] Клиент не получил данных 20 сек, отключение")
                        break
                    continue
        finally:
            with clients_lock:
                if client_q in clients:
                    clients.remove(client_q)
            print(f"[radio] - Слушатель (всего: {len(clients)})")

    return Response(
        generate(),
        mimetype='audio/mpeg',
        headers={
            # Запрещаем любое кэширование и буферизацию на прокси
            'Cache-Control':          'no-cache, no-store, must-revalidate',
            'Pragma':                 'no-cache',
            'Expires':                '0',
            'X-Accel-Buffering':      'no',   # отключает буфер nginx
            'X-Content-Type-Options': 'nosniff',
            'Transfer-Encoding':      'chunked',
            'Connection':             'keep-alive',
            # ICY-заголовки для совместимости с радио-клиентами / OBS
            'icy-br':      str(current_song.get('bitrate', 128)),
            'icy-genre':   'Various',
            'icy-name':    'MLP Radio',
            'icy-pub':     '0',
        }
    )


@app.route('/title')
def title():
    return (
        current_song["title"],
        200,
        {
            'Content-Type':  'text/plain; charset=utf-8',
            'Cache-Control': 'no-cache',
            'Access-Control-Allow-Origin': '*',
        }
    )


@app.route('/title-page')
def title_page():
    """Страница с одним текстом трека, обновляется через AJAX. Никакого SSE."""
    return render_template('title.html')


@app.route('/title-stream')
def title_stream():
    """SSE — название трека в реальном времени (для JS на странице)."""
    q: queue.Queue = queue.Queue(maxsize=16)

    with clients_lock:
        title_listeners.append(q)

    def generate():
        # Текущий трек сразу при подключении
        yield f"data: {current_song['title']}\n\n"
        try:
            while not shutdown_event.is_set():
                try:
                    data = q.get(timeout=1.0)
                    yield data
                except queue.Empty:
                    # Heartbeat — держим соединение живым через прокси
                    yield ": heartbeat\n\n"
        finally:
            with clients_lock:
                if q in title_listeners:
                    title_listeners.remove(q)

    return Response(
        generate(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control':             'no-cache',
            'X-Accel-Buffering':         'no',
            'Access-Control-Allow-Origin': '*',
        }
    )


@app.route('/status')
def status():
    with clients_lock:
        listeners = len(clients)
    return (
        {
            "song":      current_song["title"],
            "bitrate":   current_song.get("bitrate", 128),
            "listeners": listeners,
        },
        200,
        {
            'Content-Type':              'application/json',
            'Access-Control-Allow-Origin': '*',
        }
    )


@app.route('/playlist')
def playlist():
    """Возвращает текущий трек, очередь и полный список песен."""
    try:
        all_songs = [f for f in os.listdir(MUSIC_DIR) if f.lower().endswith('.mp3')]
        all_songs.sort(key=lambda f: clean_title(f).lower())
        
        songs_data = []
        for filename in all_songs:
            songs_data.append({
                "filename": filename,
                "title": clean_title(filename)
            })
        
        return (
            {
                "current": current_song["title"],
                "queue": [],  # Очередь будет пуста, т.к. трек выбирается случайно
                "all_songs": songs_data,
                "total": len(songs_data)
            },
            200,
            {
                'Content-Type':              'application/json',
                'Access-Control-Allow-Origin': '*',
            }
        )
    except FileNotFoundError:
        return (
            {"error": "Music directory not found"},
            404,
            {
                'Content-Type':              'application/json',
                'Access-Control-Allow-Origin': '*',
            }
        )


# ─── Запуск ───────────────────────────────────────────────────

def signal_handler(sig, frame):
    print("\n[radio] Завершение...")
    shutdown_event.set()
    sys.exit(0)


if __name__ == '__main__':
    signal.signal(signal.SIGINT,  signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    thread = threading.Thread(target=broadcaster, daemon=True)
    thread.start()

    print(f"\n  Главная:       http://localhost:{PORT}/")
    print(f"  Стрим:         http://localhost:{PORT}/stream")
    print(f"  Трек (текст):  http://localhost:{PORT}/title")
    print(f"  Трек (страница): http://localhost:{PORT}/title-page")
    print(f"  Трек (SSE):    http://localhost:{PORT}/title-stream")
    print(f"  Статус:        http://localhost:{PORT}/status\n")

    app.run(host=HOST, port=PORT, threaded=True)
