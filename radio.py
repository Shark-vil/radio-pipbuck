# radio.py
# pip install flask mutagen flask-minify python-dotenv

import os
from dotenv import load_dotenv
load_dotenv()
import re
import json
import random
import threading
import queue
import collections
import time
import signal
import sys
from flask import Flask, Response, render_template, abort
from flask_minify import Minify
from mutagen.mp3 import MP3

# ─── Настройки ────────────────────────────────────────────────
MUSIC_DIR    = os.environ.get("MUSIC_DIR", r"D:\mlpStream")
HOST         = "0.0.0.0"
PORT         = int(os.environ.get("PORT", 8000))
TITLE_DELAY  = float(os.environ.get("TITLE_DELAY_SECONDS", "5"))  # 0 = без задержки

# Общая станция — доступна на "/" (выбор станции) и "/<STATION_ID>/",
# а её эндпоинты дополнительно продублированы без префикса для обратной совместимости
DEFAULT_STATION_ID   = "general"
DEFAULT_STATION_NAME = os.environ.get("STATION_NAME", "MLP Radio")

# Кастомные станции описываются JSON-файлами в этой папке (см. README)
STATIONS_DIR = os.environ.get("STATIONS_DIR", "stations")

# Идентификаторы, которые нельзя использовать как id станции — конфликтуют с системными роутами
RESERVED_STATION_IDS = {
    "pages", "stream", "title", "title-page", "title-stream",
    "status", "playlist", "obs", "static", "api",
}
_STATION_ID_RE = re.compile(r'^[a-zA-Z0-9_-]+$')

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

shutdown_event = threading.Event()

# ──────────────────────────────────────────────────────────────

app = Flask(__name__)
Minify(app=app, html=True, js=True, cssless=True)


def clean_title(filename: str) -> str:
    name = os.path.splitext(filename)[0]
    name = re.sub(r'^\d+\s*[-—–]\s*', '', name).strip()
    return name


# ─── Станция ──────────────────────────────────────────────────
# Каждая станция — независимый плейлист со своим broadcaster-потоком,
# списком слушателей, очередью воспроизведения и пребуфером.

class Station:
    def __init__(self, station_id: str, name: str, music_dir: str, files: list[str] | None = None):
        self.id = station_id
        self.name = name
        self.music_dir = music_dir
        self.explicit_files = files  # None → крутится вся папка music_dir

        self.current_song: dict = {"title": "—", "bitrate": 128}
        self.clients: list = []
        self.title_listeners: list = []
        self.clients_lock = threading.Lock()

        self.song_cache: list[dict] = []
        self.song_cache_lock = threading.Lock()

        self.play_queue: list = []
        self.queue_lock = threading.Lock()
        self.queue_index = 0
        self.next_queue_ready = threading.Event()

        self._queue_generation_in_progress = False
        self._queue_gen_lock = threading.Lock()

        self._audio_prebuffer: collections.deque = collections.deque()
        self._audio_prebuffer_size = 0
        self._audio_prebuffer_lock = threading.Lock()

    def refresh_song_cache(self) -> list[dict]:
        """Сканирует music_dir (или фильтрует по явному списку файлов) и кэширует результат."""
        try:
            available = set(os.listdir(self.music_dir))
        except FileNotFoundError:
            print(f"[radio:{self.id}] Папка не найдена: {self.music_dir}")
            with self.song_cache_lock:
                self.song_cache.clear()
            return []

        if self.explicit_files is not None:
            files = []
            for f in self.explicit_files:
                if f not in available:
                    print(f"[radio:{self.id}] Файл из плейлиста не найден: {f}")
                    continue
                if not f.lower().endswith('.mp3'):
                    continue
                files.append(f)
        else:
            files = [f for f in available if f.lower().endswith('.mp3')]

        files.sort(key=lambda f: clean_title(f).lower())
        cached = [{"filename": f, "title": clean_title(f)} for f in files]
        with self.song_cache_lock:
            self.song_cache.clear()
            self.song_cache.extend(cached)
        return cached

    def get_song_cache(self) -> list[dict]:
        with self.song_cache_lock:
            cached = list(self.song_cache)
        if not cached:
            return self.refresh_song_cache()
        return cached

    def build_prepared_queue(self) -> list:
        """Создаёт перемешанную очередь треков с предзагруженными метаданными."""
        cached = self.get_song_cache()
        if not cached:
            return []

        filenames = [s["filename"] for s in cached]
        random.shuffle(filenames)

        prepared = []
        for filename in filenames:
            filepath = os.path.join(self.music_dir, filename)
            try:
                audio_info = MP3(filepath)
                bitrate = audio_info.info.bitrate
            except Exception as e:
                print(f"[radio:{self.id}] Не удалось прочитать {filename}: {e}")
                bitrate = 128_000
            prepared.append((filename, filepath, bitrate))

        return prepared

    def get_current_queue(self) -> list[dict]:
        """Возвращает список оставшихся треков в текущей очереди."""
        with self.queue_lock:
            current_idx = self.queue_index
            queue_snapshot = list(self.play_queue)

        upcoming = []
        for i, (filename, filepath, bitrate) in enumerate(queue_snapshot):
            if i >= current_idx:
                upcoming.append({
                    "title": clean_title(filename),
                    "position": i - current_idx + 1
                })
        return upcoming

    def notify_title_change(self) -> None:
        """SSE-уведомление о смене трека + обновлении очереди."""
        payload = json.dumps({
            "title": self.current_song["title"],
            "queue": self.get_current_queue(),
            "queue_position": self.queue_index + 1,
            "queue_total": len(self.play_queue)
        })
        sse_data = f"data: {payload}\n\n"
        dead: list = []
        with self.clients_lock:
            for q in self.title_listeners:
                try:
                    q.put_nowait(sse_data)
                except queue.Full:
                    dead.append(q)
            for d in dead:
                if d in self.title_listeners:
                    self.title_listeners.remove(d)

    def _broadcast_silence(self, bitrate_kbps: int, count: int) -> None:
        """Рассылает `count` тихих MPEG-фреймов всем клиентам и в пребуфер.
        Вызывается только из broadcaster-потока, чтобы заполнить паузу между треками."""
        frame = _SILENT_FRAMES.get(bitrate_kbps, _SILENT_FRAMES[128])
        with self.clients_lock:
            snapshot = list(self.clients)
        for _ in range(count):
            for q in snapshot:
                try:
                    q.put_nowait(frame)
                except queue.Full:
                    pass
        with self._audio_prebuffer_lock:
            for _ in range(count):
                self._audio_prebuffer.append(frame)
                self._audio_prebuffer_size += len(frame)
            while self._audio_prebuffer_size > PREBUFFER_MAX_BYTES:
                removed = self._audio_prebuffer.popleft()
                self._audio_prebuffer_size -= len(removed)

    def _prepare_next_queue_safe(self) -> None:
        """Генерирует следующую очередь в фоновом потоке (с safe-флагом)."""
        try:
            print(f"[radio:{self.id}] Генерация следующей очереди...")
            new_queue = self.build_prepared_queue()
            if new_queue:
                with self.queue_lock:
                    self.play_queue = new_queue
                    self.next_queue_ready.set()
                print(f"[radio:{self.id}] Следующая очередь готова ({len(new_queue)} треков)")
            else:
                print(f"[radio:{self.id}] Не удалось создать следующую очередь (нет файлов?)")
        finally:
            with self._queue_gen_lock:
                self._queue_generation_in_progress = False

    def broadcaster(self) -> None:
        print(f"[radio:{self.id}] Стрим «{self.name}» запущен. Папка: {self.music_dir}")

        self.refresh_song_cache()
        self.play_queue = self.build_prepared_queue()
        self.next_queue_ready.clear()

        while not shutdown_event.is_set():
            if not self.play_queue:
                print(f"[radio:{self.id}] Нет MP3-файлов, жду 10 сек...")
                shutdown_event.wait(10)
                self.refresh_song_cache()
                self.play_queue = self.build_prepared_queue()
                continue

            for self.queue_index in range(len(self.play_queue)):
                if shutdown_event.is_set():
                    break

                filename, filepath, bitrate = self.play_queue[self.queue_index]

                # Если это предпоследний трек — запускаем генерацию следующей очереди в фоне
                if self.queue_index == len(self.play_queue) - 2 and len(self.play_queue) >= 2:
                    with self._queue_gen_lock:
                        if not self._queue_generation_in_progress and not self.next_queue_ready.is_set():
                            self._queue_generation_in_progress = True
                            threading.Thread(target=self._prepare_next_queue_safe, daemon=True).start()

                self.current_song["title"]   = clean_title(filename)
                self.current_song["bitrate"] = bitrate // 1000
                print(f"[radio:{self.id}] ▶ {self.current_song['title']}  [{self.current_song['bitrate']} kbps  ({self.queue_index + 1}/{len(self.play_queue)})]")
                if TITLE_DELAY > 0:
                    threading.Timer(TITLE_DELAY, self.notify_title_change).start()
                else:
                    self.notify_title_change()

                bytes_per_sec = bitrate / 8  # байт/с

                try:
                    with open(filepath, 'rb') as f:
                        start_time = time.perf_counter()
                        bytes_sent = 0

                        while not shutdown_event.is_set():
                            chunk = f.read(CHUNK)
                            if not chunk:
                                break

                            with self.clients_lock:
                                snapshot = list(self.clients)

                            dead_clients: list = []
                            for q in snapshot:
                                try:
                                    q.put_nowait(chunk)
                                except queue.Full:
                                    dead_clients.append(q)

                            if dead_clients:
                                with self.clients_lock:
                                    for dead_q in dead_clients:
                                        if dead_q in self.clients:
                                            self.clients.remove(dead_q)
                                print(f"[radio:{self.id}] Дроп медленных клиентов: {len(dead_clients)}")

                            # Записываем чанк в пребуфер для новых клиентов
                            with self._audio_prebuffer_lock:
                                self._audio_prebuffer.append(chunk)
                                self._audio_prebuffer_size += len(chunk)
                                while self._audio_prebuffer_size > PREBUFFER_MAX_BYTES:
                                    removed = self._audio_prebuffer.popleft()
                                    self._audio_prebuffer_size -= len(removed)

                            bytes_sent += len(chunk)

                            expected = bytes_sent / bytes_per_sec
                            elapsed  = time.perf_counter() - start_time
                            drift    = expected - elapsed
                            if drift > 0.02:
                                shutdown_event.wait(drift)

                except Exception as e:
                    print(f"[radio:{self.id}] Ошибка при чтении {filename}: {e}")

                # Инжектируем тишину в очереди клиентов сразу после конца трека.
                # Это гарантирует, что HTTP-поток не прерывается пока broadcaster открывает
                # следующий файл. 8 фреймов ≈ 208 мс — достаточно для любых задержек ФС.
                if not shutdown_event.is_set():
                    try:
                        next_br = self.play_queue[self.queue_index + 1][2] // 1000
                    except IndexError:
                        next_br = bitrate // 1000
                    self._broadcast_silence(next_br, 8)

            # Очередь закончилась — ждём, пока следующая будет готова
            if not shutdown_event.is_set():
                print(f"[radio:{self.id}] Очередь закончилась, жду следующую...")
                while not shutdown_event.is_set() and not self.next_queue_ready.is_set():
                    # Гоним тишину с реальным темпом, чтобы клиенты не потеряли поток
                    self._broadcast_silence(self.current_song['bitrate'], 10)
                    shutdown_event.wait(10 * 1152 / 44100)  # ~261 мс = длительность 10 фреймов

                if shutdown_event.is_set():
                    break

                self.next_queue_ready.clear()
                self.queue_index = 0
                print(f"[radio:{self.id}] Новая очередь готова ({len(self.play_queue)} треков)")

        print(f"[radio:{self.id}] Broadcaster остановлен.")


# ─── Реестр станций ───────────────────────────────────────────

stations: dict[str, Station] = {}


def load_station_configs() -> list[dict]:
    """Читает JSON-файлы кастомных станций из STATIONS_DIR (см. README)."""
    configs = []
    if not os.path.isdir(STATIONS_DIR):
        return configs

    for fname in sorted(os.listdir(STATIONS_DIR)):
        if not fname.lower().endswith('.json'):
            continue
        station_id = os.path.splitext(fname)[0]

        if not _STATION_ID_RE.match(station_id):
            print(f"[radio] Пропускаю станцию «{fname}»: id может содержать только буквы, цифры, «-» и «_»")
            continue
        if station_id in RESERVED_STATION_IDS or station_id == DEFAULT_STATION_ID:
            print(f"[radio] Пропускаю станцию «{fname}»: id «{station_id}» зарезервирован")
            continue

        path = os.path.join(STATIONS_DIR, fname)
        try:
            with open(path, encoding='utf-8') as f:
                data = json.load(f)
        except Exception as e:
            print(f"[radio] Не удалось прочитать {fname}: {e}")
            continue

        name = data.get('name') or station_id
        music_dir = data.get('music_dir') or MUSIC_DIR
        files = data.get('files')
        if files is not None and not isinstance(files, list):
            print(f"[radio] Станция «{fname}»: поле 'files' должно быть списком строк, игнорирую его")
            files = None

        configs.append({"id": station_id, "name": name, "music_dir": music_dir, "files": files})

    return configs


def load_stations() -> None:
    stations.clear()
    stations[DEFAULT_STATION_ID] = Station(DEFAULT_STATION_ID, DEFAULT_STATION_NAME, MUSIC_DIR, files=None)
    for cfg in load_station_configs():
        stations[cfg["id"]] = Station(cfg["id"], cfg["name"], cfg["music_dir"], files=cfg["files"])


def _get_station(station_id: str | None) -> Station:
    station = stations.get(station_id or DEFAULT_STATION_ID)
    if station is None:
        abort(404)
    return station


# ─── Роуты ────────────────────────────────────────────────────

@app.route('/')
def home():
    """Выбор станции."""
    station_list = [{"id": s.id, "name": s.name} for s in stations.values()]
    return render_template('stations.html', stations=station_list)


@app.route('/<station_id>/')
def station_index(station_id):
    station = _get_station(station_id)
    return render_template('index.html', station_id=station.id, station_name=station.name)


@app.route('/stream')
@app.route('/<station_id>/stream')
def stream(station_id=None):
    station = _get_station(station_id)
    client_q: queue.Queue = queue.Queue(maxsize=QUEUE_SIZE)

    # Снимаем снапшот пребуфера ДО добавления клиента в список,
    # чтобы не получить дубли от broadcaster'а
    with station._audio_prebuffer_lock:
        prebuffer_snapshot = list(station._audio_prebuffer)

    with station.clients_lock:
        station.clients.append(client_q)

    # Мгновенно отдаём новому клиенту последние ~10 сек аудио.
    # Браузер накапливает буфер до начала воспроизведения → нет статтеров.
    for cached_chunk in prebuffer_snapshot:
        try:
            client_q.put_nowait(cached_chunk)
        except queue.Full:
            break

    print(f"[radio:{station.id}] + Слушатель (всего: {len(station.clients)}, пребуфер: {len(prebuffer_snapshot)} чанков)")

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
                        print(f"[radio:{station.id}] Клиент не получил данных 20 сек, отключение")
                        break
                    continue
        finally:
            with station.clients_lock:
                if client_q in station.clients:
                    station.clients.remove(client_q)
            print(f"[radio:{station.id}] - Слушатель (всего: {len(station.clients)})")

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
            'icy-br':      str(station.current_song.get('bitrate', 128)),
            'icy-genre':   'Various',
            'icy-name':    station.name,
            'icy-pub':     '0',
        }
    )


@app.route('/title')
@app.route('/<station_id>/title')
def title(station_id=None):
    station = _get_station(station_id)
    return (
        station.current_song["title"],
        200,
        {
            'Content-Type':  'text/plain; charset=utf-8',
            'Cache-Control': 'no-cache',
            'Access-Control-Allow-Origin': '*',
        }
    )


@app.route('/title-page')
@app.route('/<station_id>/title-page')
def title_page(station_id=None):
    """Страница с одним текстом трека, обновляется через AJAX. Никакого SSE."""
    station = _get_station(station_id)
    return render_template('title.html', station_id=station.id)


@app.route('/obs')
@app.route('/<station_id>/obs')
def obs(station_id=None):
    """Прозрачная страница для OBS Browser Source — только аудио, без UI."""
    station = _get_station(station_id)
    return render_template('obs.html', station_id=station.id)


@app.route('/pages')
def pages():
    """Описание всех доступных страниц сервера."""
    station_list = [{"id": s.id, "name": s.name} for s in stations.values()]
    return render_template('pages.html', stations=station_list)


@app.route('/title-stream')
@app.route('/<station_id>/title-stream')
def title_stream(station_id=None):
    """SSE — название трека + очередь в реальном времени."""
    station = _get_station(station_id)
    q: queue.Queue = queue.Queue(maxsize=16)

    with station.clients_lock:
        station.title_listeners.append(q)

    def generate():
        # Начальное состояние: текущий трек + очередь
        initial_payload = json.dumps({
            "title": station.current_song["title"],
            "queue": station.get_current_queue(),
            "queue_position": station.queue_index + 1,
            "queue_total": len(station.play_queue)
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
            with station.clients_lock:
                if q in station.title_listeners:
                    station.title_listeners.remove(q)

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
@app.route('/<station_id>/status')
def status(station_id=None):
    station = _get_station(station_id)
    with station.clients_lock:
        listeners = len(station.clients)
    return (
        {
            "song":      station.current_song["title"],
            "bitrate":   station.current_song.get("bitrate", 128),
            "listeners": listeners,
        },
        200,
        {
            'Content-Type':              'application/json',
            'Access-Control-Allow-Origin': '*',
        }
    )


@app.route('/playlist')
@app.route('/<station_id>/playlist')
def playlist(station_id=None):
    """Возвращает текущий трек, очередь и полный список песен."""
    station = _get_station(station_id)
    all_songs = station.get_song_cache()
    upcoming = station.get_current_queue()

    return (
        {
            "current": station.current_song["title"],
            "queue": upcoming,
            "all_songs": all_songs,
            "total": len(all_songs),
            "queue_position": station.queue_index + 1,
            "queue_total": len(station.play_queue)
        },
        200,
        {
            'Content-Type':              'application/json',
            'Access-Control-Allow-Origin': '*',
        }
    )


@app.route('/api/stations')
def api_stations():
    """Список станций для клиентского кода (напр. страницы выбора)."""
    return (
        {"stations": [{"id": s.id, "name": s.name} for s in stations.values()]},
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

    load_stations()

    for station in stations.values():
        threading.Thread(target=station.broadcaster, daemon=True).start()

    print(f"\n  Выбор станции: http://localhost:{PORT}/")
    for station in stations.values():
        print(f"  Станция «{station.name}»: http://localhost:{PORT}/{station.id}/")
    print()

    app.run(host=HOST, port=PORT, threaded=True)
