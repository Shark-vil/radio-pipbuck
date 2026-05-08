# radio.py
# pip install flask mutagen flask-minify python-dotenv

import os
from dotenv import load_dotenv
load_dotenv()
import re
import random
import threading
import queue
import collections
import time
import signal
import sys
from flask import Flask, Response, render_template, jsonify
from flask_minify import Minify
from mutagen.mp3 import MP3

# ─── Настройки ────────────────────────────────────────────────
MUSIC_DIR    = os.environ.get("MUSIC_DIR", r"D:\mlpStream")
HOST         = "0.0.0.0"
PORT         = int(os.environ.get("PORT", 8000))
TITLE_DELAY  = float(os.environ.get("TITLE_DELAY_SECONDS", "5"))  # 0 = без задержки

# Маленький чанк = гранулярная отправка = плавный поток без статтеров
# 4096 байт при 128 kbps → отправка каждые ~0.25 сек (вместо ~1 сек при 16 KB)
CHUNK      = 4096
QUEUE_SIZE = 512   # ~2 MB буфер на клиента — достаточно для YouTube-ингеста

# Пребуфер для новых клиентов: ~10 сек аудио при 192 kbps
# Новый клиент сразу получает этот буфер и избавляется от статтеров при старте
PREBUFFER_MAX_BYTES = 256_000  # ~10 сек при 192 kbps, ~16 сек при 128 kbps

# Заготовленные тихие MPEG1-Layer3-фреймы для заполнения переходов между треками.
# Каждый фрейм: 4-байтный заголовок (sync + MPEG1 + Layer3 + bitrate + 44100 Hz, stereo)
# + нули в качестве аудиоданных (part2_3_length=0 → нет сэмплов → настоящая тишина).
def _make_silent_frame(bitrate_kbps: int) -> bytes:
    idx = {32:1,40:2,48:3,56:4,64:5,80:6,96:7,112:8,128:9,160:10,192:11,224:12,256:13,320:14}.get(bitrate_kbps, 9)
    frame_size = (144 * bitrate_kbps * 1000) // 44100
    return b'\xff\xfb' + bytes([idx << 4, 0x00]) + b'\x00' * (frame_size - 4)

_SILENT_FRAMES: dict = {br: _make_silent_frame(br) for br in [32,40,48,56,64,80,96,112,128,160,192,224,256,320]}

# ──────────────────────────────────────────────────────────────

app = Flask(__name__)
Minify(app=app, html=True, js=True, cssless=True)

current_song: dict    = {"title": "—", "bitrate": 128}
clients: list         = []
title_listeners: list = []
clients_lock          = threading.Lock()
shutdown_event        = threading.Event()

# Кэш списка песен (сканируем файловую систему один раз)
song_cache: list[dict] = []
song_cache_lock = threading.Lock()

# Очередь воспроизведения и управление следующей очередью
play_queue: list = []          # Текущая очередь треков
queue_lock = threading.Lock()
queue_index = 0                # Индекс текущего трека в очереди
next_queue_ready = threading.Event()  # Сигнал: следующая очередь готова

# Предотвращение двойного запуска генерации очереди
_queue_generation_in_progress = False
_queue_gen_lock = threading.Lock()

# Кольцевой пребуфер: хранит последние PREBUFFER_MAX_BYTES байт аудио
# Новые клиенты сразу получают этот буфер, что устраняет статтеры при старте
_audio_prebuffer: collections.deque = collections.deque()
_audio_prebuffer_size: int = 0
_audio_prebuffer_lock = threading.Lock()


# ─── Утилиты ──────────────────────────────────────────────────

import json as _json_module

def get_current_queue() -> list[dict]:
    """Возвращает список оставшихся треков в текущей очереди."""
    with queue_lock:
        current_idx = queue_index
        queue_snapshot = list(play_queue)

    upcoming = []
    for i, (filename, filepath, bitrate) in enumerate(queue_snapshot):
        if i >= current_idx:
            upcoming.append({
                "title": clean_title(filename),
                "position": i - current_idx + 1
            })
    return upcoming


def notify_title_change() -> None:
    """SSE-уведомление о смене трека + обновлении очереди."""
    payload = _json_module.dumps({
        "title": current_song["title"],
        "queue": get_current_queue(),
        "queue_position": queue_index + 1,
        "queue_total": len(play_queue)
    })
    sse_data = f"data: {payload}\n\n"
    dead: list = []
    with clients_lock:
        for q in title_listeners:
            try:
                q.put_nowait(sse_data)
            except queue.Full:
                dead.append(q)
        for d in dead:
            if d in title_listeners:
                title_listeners.remove(d)


def clean_title(filename: str) -> str:
    name = os.path.splitext(filename)[0]
    name = re.sub(r'^\d+\s*[-—–]\s*', '', name).strip()
    return name


def refresh_song_cache() -> list[dict]:
    """Сканирует MUSIC_DIR и возвращает отсортированный список песен. Кэширует результат."""
    try:
        files = [f for f in os.listdir(MUSIC_DIR) if f.lower().endswith('.mp3')]
        files.sort(key=lambda f: clean_title(f).lower())
        cached = [{"filename": f, "title": clean_title(f)} for f in files]
        with song_cache_lock:
            song_cache.clear()
            song_cache.extend(cached)
        return cached
    except FileNotFoundError:
        print(f"[radio] Папка не найдена: {MUSIC_DIR}")
        with song_cache_lock:
            song_cache.clear()
        return []


def get_song_cache() -> list[dict]:
    """Возвращает кэшированный список песен. Если пуст — сканирует."""
    with song_cache_lock:
        if not song_cache:
            return refresh_song_cache()
        return list(song_cache)


def build_prepared_queue() -> list:
    """Создаёт перемешанную очередь треков с предзагруженными метаданными."""
    cached = get_song_cache()
    if not cached:
        return []

    filenames = [s["filename"] for s in cached]
    random.shuffle(filenames)

    prepared = []
    for filename in filenames:
        filepath = os.path.join(MUSIC_DIR, filename)
        try:
            audio_info = MP3(filepath)
            bitrate = audio_info.info.bitrate
        except Exception as e:
            print(f"[radio] Не удалось прочитать {filename}: {e}")
            bitrate = 128_000
        prepared.append((filename, filepath, bitrate))

    return prepared


def _broadcast_silence(bitrate_kbps: int, count: int) -> None:
    """Рассылает `count` тихих MPEG-фреймов всем клиентам и в пребуфер.
    Вызывается только из broadcaster-потока, чтобы заполнить паузу между треками."""
    global _audio_prebuffer_size
    frame = _SILENT_FRAMES.get(bitrate_kbps, _SILENT_FRAMES[128])
    with clients_lock:
        snapshot = list(clients)
    for _ in range(count):
        for q in snapshot:
            try:
                q.put_nowait(frame)
            except queue.Full:
                pass
    with _audio_prebuffer_lock:
        for _ in range(count):
            _audio_prebuffer.append(frame)
            _audio_prebuffer_size += len(frame)
        while _audio_prebuffer_size > PREBUFFER_MAX_BYTES:
            removed = _audio_prebuffer.popleft()
            _audio_prebuffer_size -= len(removed)


# ─── Broadcaster ──────────────────────────────────────────────

def broadcaster() -> None:
    global play_queue, queue_index, _queue_generation_in_progress, _audio_prebuffer_size

    print(f"[radio] Стрим запущен. Папка: {MUSIC_DIR}")

    # Инициализация: кэшируем песни и создаём первую очередь
    refresh_song_cache()
    play_queue = build_prepared_queue()
    next_queue_ready.clear()

    while not shutdown_event.is_set():
        if not play_queue:
            print("[radio] Нет MP3-файлов, жду 10 сек...")
            shutdown_event.wait(10)
            refresh_song_cache()
            play_queue = build_prepared_queue()
            continue

        for queue_index in range(len(play_queue)):
            if shutdown_event.is_set():
                break

            filename, filepath, bitrate = play_queue[queue_index]

            # Если это предпоследний трек — запускаем генерацию следующей очереди в фоне
            if queue_index == len(play_queue) - 2 and len(play_queue) >= 2:
                with _queue_gen_lock:
                    if not _queue_generation_in_progress and not next_queue_ready.is_set():
                        _queue_generation_in_progress = True
                        threading.Thread(target=_prepare_next_queue_safe, daemon=True).start()

            current_song["title"]   = clean_title(filename)
            current_song["bitrate"] = bitrate // 1000
            print(f"[radio] ▶ {current_song['title']}  [{current_song['bitrate']} kbps  ({queue_index + 1}/{len(play_queue)})]")
            if TITLE_DELAY > 0:
                threading.Timer(TITLE_DELAY, notify_title_change).start()
            else:
                notify_title_change()

            bytes_per_sec = bitrate / 8  # байт/с

            try:
                with open(filepath, 'rb') as f:
                    start_time = time.perf_counter()
                    bytes_sent = 0

                    while not shutdown_event.is_set():
                        chunk = f.read(CHUNK)
                        if not chunk:
                            break

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

                        # Записываем чанк в пребуфер для новых клиентов
                        with _audio_prebuffer_lock:
                            _audio_prebuffer.append(chunk)
                            _audio_prebuffer_size += len(chunk)
                            while _audio_prebuffer_size > PREBUFFER_MAX_BYTES:
                                removed = _audio_prebuffer.popleft()
                                _audio_prebuffer_size -= len(removed)

                        bytes_sent += len(chunk)

                        expected = bytes_sent / bytes_per_sec
                        elapsed  = time.perf_counter() - start_time
                        drift    = expected - elapsed
                        if drift > 0.02:
                            shutdown_event.wait(drift)

            except Exception as e:
                print(f"[radio] Ошибка при чтении {filename}: {e}")

            # Инжектируем тишину в очереди клиентов сразу после конца трека.
            # Это гарантирует, что HTTP-поток не прерывается пока broadcaster открывает
            # следующий файл. 8 фреймов ≈ 208 мс — достаточно для любых задержек ФС.
            if not shutdown_event.is_set():
                try:
                    next_br = play_queue[queue_index + 1][2] // 1000
                except IndexError:
                    next_br = bitrate // 1000
                _broadcast_silence(next_br, 8)

        # Очередь закончилась — ждём, пока следующая будет готова
        if not shutdown_event.is_set():
            print("[radio] Очередь закончилась, жду следующую...")
            while not shutdown_event.is_set() and not next_queue_ready.is_set():
                # Гоним тишину с реальным темпом, чтобы клиенты не потеряли поток
                _broadcast_silence(current_song['bitrate'], 10)
                shutdown_event.wait(10 * 1152 / 44100)  # ~261 мс = длительность 10 фреймов

            if shutdown_event.is_set():
                break

            with queue_lock:
                pass  # play_queue уже обновлён в _prepare_next_queue_safe
            next_queue_ready.clear()
            queue_index = 0
            print(f"[radio] Новая очередь готова ({len(play_queue)} треков)")

    print("[radio] Broadcaster остановлен.")


def _prepare_next_queue_safe() -> None:
    """Генерирует следующую очередь в фоновом потоке (с safe-флагом)."""
    global play_queue, _queue_generation_in_progress

    try:
        print("[radio] Генерация следующей очереди...")
        new_queue = build_prepared_queue()
        if new_queue:
            with queue_lock:
                play_queue = new_queue
                next_queue_ready.set()
            print(f"[radio] Следующая очередь готова ({len(new_queue)} треков)")
        else:
            print("[radio] Не удалось создать следующую очередь (нет файлов?)")
    finally:
        with _queue_gen_lock:
            _queue_generation_in_progress = False


# ─── Роуты ────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/stream')
def stream():
    client_q: queue.Queue = queue.Queue(maxsize=QUEUE_SIZE)

    # Снимаем снапшот пребуфера ДО добавления клиента в список,
    # чтобы не получить дубли от broadcaster'а
    with _audio_prebuffer_lock:
        prebuffer_snapshot = list(_audio_prebuffer)

    with clients_lock:
        clients.append(client_q)

    # Мгновенно отдаём новому клиенту последние ~10 сек аудио.
    # Браузер накапливает буфер до начала воспроизведения → нет статтеров.
    for cached_chunk in prebuffer_snapshot:
        try:
            client_q.put_nowait(cached_chunk)
        except queue.Full:
            break

    print(f"[radio] + Слушатель (всего: {len(clients)}, пребуфер: {len(prebuffer_snapshot)} чанков)")

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


@app.route('/obs')
def obs():
    """Прозрачная страница для OBS Browser Source — только аудио, без UI."""
    return render_template('obs.html')


@app.route('/pages')
def pages():
    """Описание всех доступных страниц сервера."""
    return render_template('pages.html')


@app.route('/title-stream')
def title_stream():
    """SSE — название трека + очередь в реальном времени."""
    q: queue.Queue = queue.Queue(maxsize=16)

    with clients_lock:
        title_listeners.append(q)

    def generate():
        # Начальное состояние: текущий трек + очередь
        initial_payload = _json_module.dumps({
            "title": current_song["title"],
            "queue": get_current_queue(),
            "queue_position": queue_index + 1,
            "queue_total": len(play_queue)
        })
        yield f"data: {initial_payload}\n\n"
        try:
            while not shutdown_event.is_set():
                try:
                    data = q.get(timeout=1.0)
                    yield data
                except queue.Empty:
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
    all_songs = get_song_cache()
    upcoming = get_current_queue()

    return (
        {
            "current": current_song["title"],
            "queue": upcoming,
            "all_songs": all_songs,
            "total": len(all_songs),
            "queue_position": queue_index + 1,
            "queue_total": len(play_queue)
        },
        200,
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
    print(f"  OBS Source:      http://localhost:{PORT}/obs")
    print(f"  Трек (SSE):    http://localhost:{PORT}/title-stream")
    print(f"  Статус:        http://localhost:{PORT}/status\n")

    app.run(host=HOST, port=PORT, threaded=True)
