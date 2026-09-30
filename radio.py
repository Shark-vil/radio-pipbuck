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

# Как часто проверять папки с музыкой и STATIONS_DIR на изменения (0 = не следить)
RELOAD_INTERVAL = float(os.environ.get("RELOAD_INTERVAL_SECONDS", "5"))

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

        # Следующая очередь готовится заранее и подменяет текущую только когда та закончится.
        # next_queue_version — версия библиотеки, из которой она собрана: если файлы успели
        # поменяться, очередь считается устаревшей и пересобирается.
        self.next_queue: list = []
        self.next_queue_version = -1
        self.next_queue_ready = threading.Event()
        self.library_version = 0

        self._queue_generation_in_progress = False
        self._queue_gen_lock = threading.Lock()

        # Последний набор файлов папки {имя: (размер, mtime)}, применённый LibraryWatcher'ом.
        # None — ещё не применялся (или сменился music_dir/files и нужно применить заново).
        self._library: dict | None = None

        self.stop_event = threading.Event()

        self._audio_prebuffer: collections.deque = collections.deque()
        self._audio_prebuffer_size = 0
        self._audio_prebuffer_lock = threading.Lock()

    def _stopping(self) -> bool:
        return shutdown_event.is_set() or self.stop_event.is_set()

    def stop(self) -> None:
        self.stop_event.set()

    def reconfigure(self, name: str, music_dir: str, files: list[str] | None) -> None:
        """Применяет изменённый JSON-конфиг станции, не останавливая эфир."""
        if name != self.name:
            print(f"[radio:{self.id}] Станция переименована: «{self.name}» → «{name}»")
            self.name = name
        if music_dir != self.music_dir or files != self.explicit_files:
            print(f"[radio:{self.id}] Плейлист станции изменён, обновляю очередь")
            self.music_dir = music_dir
            self.explicit_files = files
            self._library = None  # LibraryWatcher применит новый набор файлов в этом же проходе

    def refresh_song_cache(self, available: set[str] | None = None) -> list[dict]:
        """Сканирует music_dir (или фильтрует по явному списку файлов) и кэширует результат.
        available — уже известный набор файлов папки (от LibraryWatcher), чтобы не сканировать заново."""
        if available is None and self._library is not None:
            available = set(self._library)
        if available is None:
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
        return self._prepare_tracks(filenames)

    def _prepare_tracks(self, filenames: list[str]) -> list:
        """Читает битрейт каждого файла → [(filename, filepath, bitrate), ...]."""
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

    def apply_library_change(self, library: dict) -> None:
        """Применяет изменившийся набор файлов папки без остановки эфира: из оставшейся части
        текущей очереди убираются пропавшие треки, новые вставляются в случайные места.
        Уже сыгранные треки и текущий трек не трогаются."""
        prev = self._library
        self._library = library
        # Файлы, перезаписанные под тем же именем, — битрейт мог поменяться, перечитываем
        changed = {n for n, sig in library.items() if prev is not None and n in prev and prev[n] != sig}

        with self.song_cache_lock:
            old_names = {s["filename"] for s in self.song_cache}
        names = {s["filename"] for s in self.refresh_song_cache(set(library))}

        if prev is not None:
            if names == old_names and not changed:
                return
            added, removed = names - old_names, old_names - names
            print(f"[radio:{self.id}] Библиотека изменилась: +{len(added)} / -{len(removed)} / ~{len(changed)}")
            for n in sorted(added):
                print(f"[radio:{self.id}]   + {clean_title(n)}")
            for n in sorted(removed):
                print(f"[radio:{self.id}]   - {clean_title(n)}")

        def is_valid(track) -> bool:
            filename, filepath, _ = track
            return (filename in names and filename not in changed
                    and filepath == os.path.join(self.music_dir, filename))

        def split_queue() -> tuple[list, list, set]:
            head = self.play_queue[:self.queue_index + 1]  # сыгранные + текущий
            kept = [t for t in self.play_queue[self.queue_index + 1:] if is_valid(t)]
            present = {t[0] for t in head if t[1] == os.path.join(self.music_dir, t[0])}
            present |= {t[0] for t in kept}
            return head, kept, present

        with self.queue_lock:
            _, _, present = split_queue()
        # Теги читаем вне блокировки — это дисковый I/O
        prepared = self._prepare_tracks(sorted(names - present))

        with self.queue_lock:
            # Пока читали теги, broadcaster мог уйти вперёд — делим очередь заново
            head, kept, present = split_queue()
            for track in prepared:
                if track[0] not in present:
                    kept.insert(random.randint(0, len(kept)), track)
            self.play_queue = head + kept

            # Заготовленная следующая очередь собрана из старого набора файлов
            self.library_version += 1
            next_was_ready = self.next_queue_ready.is_set()
            if next_was_ready:
                self.next_queue_ready.clear()
                self.next_queue = []

        if next_was_ready:
            self._request_next_queue()
        self.notify_title_change()

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

    def _request_next_queue(self) -> None:
        """Запускает фоновую генерацию следующей очереди, если она ещё не готова и не готовится."""
        with self._queue_gen_lock:
            if self._queue_generation_in_progress or self.next_queue_ready.is_set():
                return
            self._queue_generation_in_progress = True
        threading.Thread(target=self._prepare_next_queue_safe, daemon=True).start()

    def _prepare_next_queue_safe(self) -> None:
        """Генерирует следующую очередь в фоновом потоке (с safe-флагом)."""
        try:
            print(f"[radio:{self.id}] Генерация следующей очереди...")
            version = self.library_version
            new_queue = self.build_prepared_queue()
            if new_queue:
                with self.queue_lock:
                    self.next_queue = new_queue
                    self.next_queue_version = version
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
        initial_queue = self.build_prepared_queue()
        with self.queue_lock:
            self.play_queue = initial_queue
            self.queue_index = 0
        self.next_queue_ready.clear()

        while not self._stopping():
            with self.queue_lock:
                queue_empty = not self.play_queue
            if queue_empty:
                print(f"[radio:{self.id}] Нет MP3-файлов, жду 10 сек...")
                self.stop_event.wait(10)
                self.refresh_song_cache()
                new_queue = self.build_prepared_queue()
                with self.queue_lock:
                    self.play_queue = new_queue
                    self.queue_index = 0
                continue

            # Очередь может меняться на ходу (LibraryWatcher добавляет/убирает треки после
            # текущего), поэтому длину перечитываем перед каждым треком
            while not self._stopping():
                with self.queue_lock:
                    if self.queue_index >= len(self.play_queue):
                        break
                    filename, filepath, bitrate = self.play_queue[self.queue_index]
                    queue_total = len(self.play_queue)

                # С предпоследнего трека — генерируем следующую очередь в фоне
                if self.queue_index >= queue_total - 2:
                    self._request_next_queue()

                self.current_song["title"]   = clean_title(filename)
                self.current_song["bitrate"] = bitrate // 1000
                print(f"[radio:{self.id}] ▶ {self.current_song['title']}  [{self.current_song['bitrate']} kbps  ({self.queue_index + 1}/{queue_total})]")
                if TITLE_DELAY > 0:
                    threading.Timer(TITLE_DELAY, self.notify_title_change).start()
                else:
                    self.notify_title_change()

                bytes_per_sec = bitrate / 8  # байт/с

                try:
                    with open(filepath, 'rb') as f:
                        start_time = time.perf_counter()
                        bytes_sent = 0

                        while not self._stopping():
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
                                self.stop_event.wait(drift)

                except Exception as e:
                    print(f"[radio:{self.id}] Ошибка при чтении {filename}: {e}")

                # Инжектируем тишину в очереди клиентов сразу после конца трека.
                # Это гарантирует, что HTTP-поток не прерывается пока broadcaster открывает
                # следующий файл. 8 фреймов ≈ 208 мс — достаточно для любых задержек ФС.
                if not self._stopping():
                    with self.queue_lock:
                        next_idx = self.queue_index + 1
                        next_track = self.play_queue[next_idx] if next_idx < len(self.play_queue) else None
                    next_br = (next_track[2] if next_track else bitrate) // 1000
                    self._broadcast_silence(next_br, 8)

                with self.queue_lock:
                    self.queue_index += 1

            if self._stopping():
                break

            with self.queue_lock:
                self.queue_index = max(len(self.play_queue) - 1, 0)

            # Очередь закончилась — ждём следующую, собранную из актуального набора файлов
            print(f"[radio:{self.id}] Очередь закончилась, жду следующую...")
            next_fresh = False
            while not self._stopping():
                with self.queue_lock:
                    next_fresh = (self.next_queue_ready.is_set()
                                  and self.next_queue_version == self.library_version)
                if next_fresh:
                    break
                if self.next_queue_ready.is_set():
                    # Библиотека изменилась, пока очередь готовилась — собираем заново
                    self.next_queue_ready.clear()
                with self._queue_gen_lock:
                    generating = self._queue_generation_in_progress
                if not generating:
                    with self.song_cache_lock:
                        library_empty = not self.song_cache
                    if library_empty:
                        break  # все файлы удалены — внешний цикл подождёт их появления
                    self._request_next_queue()

                # Гоним тишину с реальным темпом, чтобы клиенты не потеряли поток
                self._broadcast_silence(self.current_song['bitrate'], 10)
                self.stop_event.wait(10 * 1152 / 44100)  # ~261 мс = длительность 10 фреймов

            if self._stopping():
                break

            with self.queue_lock:
                self.play_queue = self.next_queue if next_fresh else []
                self.next_queue = []
                self.queue_index = 0
            self.next_queue_ready.clear()
            if next_fresh:
                print(f"[radio:{self.id}] Новая очередь готова ({len(self.play_queue)} треков)")

        print(f"[radio:{self.id}] Broadcaster остановлен.")


# ─── Реестр станций ───────────────────────────────────────────

stations: dict[str, Station] = {}


def load_station_configs() -> list[tuple[str, dict | None]]:
    """Читает JSON-файлы кастомных станций из STATIONS_DIR (см. README).
    Возвращает [(station_id, config), ...]; config = None, если файл не удалось прочитать
    (например, его как раз сохраняют) — тогда у запущенной станции остаётся прежний конфиг."""
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
            if not isinstance(data, dict):
                raise ValueError("ожидается JSON-объект")
        except Exception as e:
            print(f"[radio] Не удалось прочитать {fname}: {e}")
            configs.append((station_id, None))
            continue

        name = data.get('name') or station_id
        music_dir = data.get('music_dir') or MUSIC_DIR
        files = data.get('files')
        if files is not None and not isinstance(files, list):
            print(f"[radio] Станция «{fname}»: поле 'files' должно быть списком строк, игнорирую его")
            files = None

        configs.append((station_id, {"name": name, "music_dir": music_dir, "files": files}))

    return configs


def start_station(station: Station) -> None:
    threading.Thread(target=station.broadcaster, daemon=True).start()


def load_stations() -> None:
    global stations
    loaded = {DEFAULT_STATION_ID: Station(DEFAULT_STATION_ID, DEFAULT_STATION_NAME, MUSIC_DIR, files=None)}
    for station_id, cfg in load_station_configs():
        if cfg is not None:
            loaded[station_id] = Station(station_id, cfg["name"], cfg["music_dir"], files=cfg["files"])
    stations = loaded


def _scan_signature(path: str, extension: str) -> dict | None:
    """{имя: (размер, mtime)} для файлов с нужным расширением; None — папки нет."""
    try:
        with os.scandir(path) as it:
            result = {}
            for entry in it:
                if entry.name.lower().endswith(extension) and entry.is_file():
                    # os.stat, а не entry.stat(): на Windows последний берёт размер из каталога,
                    # который для дописываемого файла отстаёт
                    st = os.stat(entry.path)
                    result[entry.name] = (st.st_size, st.st_mtime_ns)
            return result
    except OSError:
        return None


class LibraryWatcher:
    """Раз в RELOAD_INTERVAL сек проверяет STATIONS_DIR и папки с музыкой и применяет
    изменения на лету: новые/удалённые/изменённые станции, добавленные/удалённые треки.

    Опрос, а не inotify — надёжно работает в Docker с bind-mount и без лишних зависимостей.
    Новый MP3 попадает в эфир только когда его размер и mtime не менялись между двумя
    проверками — так недокачанный файл не уйдёт в эфир обрезанным."""

    def __init__(self):
        self._stations_sig: dict | None = None
        self._prev_scan: dict[str, dict] = {}   # music_dir → скан с прошлой проверки
        self._applied: dict[str, dict] = {}     # music_dir → набор файлов, отданный станциям

    def run(self) -> None:
        print(f"[radio] Слежу за изменениями файлов (раз в {RELOAD_INTERVAL:g} сек)")
        while not shutdown_event.wait(RELOAD_INTERVAL):
            try:
                self.check()
            except Exception as e:
                print(f"[radio] Ошибка при проверке изменений: {e}")

    def check(self) -> None:
        self._check_station_configs()
        self._check_music_dirs()

    def _check_station_configs(self) -> None:
        global stations
        sig = _scan_signature(STATIONS_DIR, '.json') or {}
        if sig == self._stations_sig:
            return
        first_check = self._stations_sig is None
        self._stations_sig = sig
        if first_check:
            return  # при старте станции уже загружены load_stations()

        current = stations
        updated = {DEFAULT_STATION_ID: current[DEFAULT_STATION_ID]}
        for station_id, cfg in load_station_configs():
            station = current.get(station_id)
            if cfg is None:
                if station is not None:
                    updated[station_id] = station
                continue
            if station is None:
                station = Station(station_id, cfg["name"], cfg["music_dir"], files=cfg["files"])
                print(f"[radio] Новая станция «{station.name}»: /{station_id}/")
                start_station(station)
            else:
                station.reconfigure(cfg["name"], cfg["music_dir"], cfg["files"])
            updated[station_id] = station

        # Подменяем словарь целиком, чтобы роуты не видели его в промежуточном состоянии
        stations = updated

        for station_id, station in current.items():
            if station_id not in updated:
                print(f"[radio] Станция «{station.name}» удалена: /{station_id}/")
                station.stop()

    def _check_music_dirs(self) -> None:
        current = stations
        music_dirs = {s.music_dir for s in current.values()}

        for music_dir in music_dirs:
            scan = _scan_signature(music_dir, '.mp3')
            if scan is None:
                scan = {}
            prev = self._prev_scan.get(music_dir)
            applied = self._applied.get(music_dir)
            self._prev_scan[music_dir] = scan

            if prev is None or applied is None:
                # Папку видим впервые — берём как есть
                library = dict(scan)
            else:
                library = {}
                for name, sig in scan.items():
                    if prev.get(name) == sig:
                        library[name] = sig            # файл не менялся с прошлой проверки
                    elif name in applied:
                        library[name] = applied[name]  # уже в эфире, ждём, пока допишется
            self._applied[music_dir] = library

            for station in current.values():
                if station.music_dir == music_dir and station._library != library:
                    station.apply_library_change(library)

        for music_dir in list(self._prev_scan):
            if music_dir not in music_dirs:
                del self._prev_scan[music_dir]
                self._applied.pop(music_dir, None)


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
            while not shutdown_event.is_set() and not station.stop_event.is_set():
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
            while not shutdown_event.is_set() and not station.stop_event.is_set():
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
    for station in stations.values():
        station.stop()
    sys.exit(0)


if __name__ == '__main__':
    signal.signal(signal.SIGINT,  signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    load_stations()

    for station in stations.values():
        start_station(station)

    if RELOAD_INTERVAL > 0:
        threading.Thread(target=LibraryWatcher().run, daemon=True).start()

    print(f"\n  Выбор станции: http://localhost:{PORT}/")
    for station in stations.values():
        print(f"  Станция «{station.name}»: http://localhost:{PORT}/{station.id}/")
    print()

    app.run(host=HOST, port=PORT, threaded=True)
