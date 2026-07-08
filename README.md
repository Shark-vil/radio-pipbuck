# radio-pipbuck

Lightweight internet radio streaming server. Broadcasts MP3 files from a local directory to multiple clients simultaneously with real-time track metadata, playlist browsing, and a modern glassmorphic web player.

## Features

- **Multiple radio stations** — run a general station plus any number of custom stations, each with its own playlist, listeners, and shareable link; a station selector is served on the home page
- **Continuous streaming** — granular chunk-based MP3 broadcasting with drift correction for accurate playback speed
- **Multi-client support** — serves unlimited concurrent listeners with per-client queue buffers and automatic slow-client detection
- **Smart queue management** — randomized shuffle playback; the next queue is pre-generated in the background before the current one ends, ensuring zero stream interruption
- **Audio pre-buffer** — new listeners instantly receive the last ~10 seconds of audio, giving the browser enough data to buffer before playback starts and eliminating startup stutters
- **Real-time metadata** — Server-Sent Events (SSE) push live track titles and queue updates to all connected clients with a configurable delay to stay in sync with actual audio playback
- **Web player** — play/pause, volume slider with touch support, live status badge, listener count
- **Playlist panel** — browse all songs with search, view the current playback queue, all without interrupting the stream
- **Localization** — automatic language detection (Russian / English) for the entire UI
- **HTML/CSS/JS minification** — responses are automatically minified on the fly via flask-minify
- **Minimal "Now Playing" page** — lightweight title display with AJAX polling for embeds or overlays
- **OBS Browser Source page** — transparent, UI-free audio player at full volume with multi-layer watchdog logic for uninterrupted stream capture
- **Docker-ready** — one-command deployment with `docker compose`

## Quick Start

### Prerequisites

- Python 3.12+
- A directory with `.mp3` files

### Environment Variables

Copy `.env.example` to `.env` and adjust as needed.

| Variable               | Default        | Description                                                                 |
|------------------------|----------------|-----------------------------------------------------------------------------|
| `MUSIC_DIR`            | `D:\mlpStream` | Path to directory with MP3 files for the general station                   |
| `PORT`                 | `8000`         | HTTP server port                                                            |
| `TITLE_DELAY_SECONDS`  | `5`            | SSE title update delay in seconds. Compensates for browser audio buffering. Set to `0` to disable. |
| `STATION_NAME`         | `MLP Radio`    | Display name of the general station                                        |
| `STATIONS_DIR`         | `stations`     | Directory scanned for custom station config files (see [Radio Stations](#radio-stations)) |

### Method 1 — Windows Launcher

```bat
run.bat
```

Creates a virtual environment, installs dependencies, and starts the server.

### Method 2 — Manual

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # Linux/macOS
pip install -r requirements.txt

cp .env.example .env            # configure as needed
python radio.py
```

### Method 3 — Docker

```bash
mkdir music                       # place your .mp3 files here
cp .env.example .env              # configure as needed
docker compose up -d
```

The server is available at `http://localhost:8000`. `docker-compose.yml` also mounts `./stations` into the container, so custom station configs work out of the box — just make sure each station's `music_dir` points inside `/music` (e.g. `/music/retro`), since that's the only host folder mounted by default.

## Radio Stations

The server always runs a **general station**, streaming everything found in `MUSIC_DIR` — this is the same behavior as before. On top of it, you can define any number of **custom stations**, each with its own name and playlist, reachable at its own URL and selectable from the home page (`/`).

### Defining a custom station

Drop a `.json` file into the `stations/` directory (configurable via `STATIONS_DIR`). The filename (without extension) becomes the station's URL id, so `stations/retro.json` is served at `/retro/`. Restart the server to pick up new or changed files.

```json
{
  "name": "Retro FM",
  "music_dir": "D:\\Music\\Retro",
  "files": [
    "01 - Track One.mp3",
    "02 - Track Two.mp3"
  ]
}
```

| Field        | Required | Description                                                                                   |
|--------------|----------|-------------------------------------------------------------------------------------------------|
| `name`       | No       | Display name shown in the player and station selector. Defaults to the station id.             |
| `music_dir`  | No       | Directory the listed (or scanned) MP3 files live in. Defaults to `MUSIC_DIR`.                  |
| `files`      | No       | Explicit list of filenames (relative to `music_dir`) to shuffle and play. If omitted, every `.mp3` file in `music_dir` is used — same as the general station. |

JSON was chosen over `.txt` / `.yml` / `.cfg` because it needs no extra dependency, parses unambiguously, and the format above is already the simplest shape that expresses "name + list of files".

A minimal config that just points at a folder (no explicit track list) also works:

```json
{ "name": "Lo-fi Beats", "music_dir": "D:\\Music\\lofi" }
```

Station ids must be alphanumeric (`-`/`_` allowed) and can't collide with reserved routes (`pages`, `stream`, `title`, `status`, `playlist`, `obs`, `static`, `api`, `general`).

### Station URLs

Every station — general or custom — exposes the same set of routes under `/<station_id>/`:

```
/<station_id>/            → player page
/<station_id>/stream      → audio stream
/<station_id>/title       → current track (plain text)
/<station_id>/title-page  → minimal now-playing page
/<station_id>/title-stream→ SSE track/queue updates
/<station_id>/status      → JSON status
/<station_id>/playlist    → JSON playlist + queue
/<station_id>/obs         → OBS Browser Source
```

The general station's id is `general` (e.g. `/general/stream`). For backward compatibility, its routes are **also** served unprefixed at the top level (`/stream`, `/title`, `/obs`, etc.) — existing bookmarks, OBS scenes, and embeds keep working unchanged.

## API Endpoints

| Method | Path                         | Description                                     | Content-Type        |
|--------|------------------------------|--------------------------------------------------|---------------------|
| GET    | `/`                          | Station selector                                | `text/html`         |
| GET    | `/<station_id>/`             | Station web player                              | `text/html`         |
| GET    | `/<station_id>/stream`       | Continuous audio stream for the station         | `audio/mpeg`        |
| GET    | `/<station_id>/title`        | Current track title                             | `text/plain`        |
| GET    | `/<station_id>/title-page`   | Minimal "Now Playing" page (AJAX)               | `text/html`         |
| GET    | `/<station_id>/title-stream` | SSE: title + queue updates                      | `text/event-stream` |
| GET    | `/<station_id>/status`       | Station status                                  | `application/json`  |
| GET    | `/<station_id>/playlist`     | Full playlist and current queue for the station | `application/json`  |
| GET    | `/<station_id>/obs`          | OBS Browser Source (transparent audio)          | `text/html`         |
| GET    | `/api/stations`              | List of all stations (id + name)                | `application/json`  |
| GET    | `/pages`                     | All pages with descriptions                     | `text/html`         |

`/stream`, `/title`, `/title-page`, `/title-stream`, `/status`, `/playlist`, and `/obs` also work unprefixed as aliases for the general station.

### `/status`

```json
{
  "song": "Track Name",
  "bitrate": 128,
  "listeners": 5
}
```

### `/playlist`

```json
{
  "current": "Currently Playing Track",
  "queue": [
    { "title": "Currently Playing Track", "position": 1 },
    { "title": "Next Track",              "position": 2 },
    { "title": "After That",              "position": 3 }
  ],
  "all_songs": [
    { "filename": "01 - Song.mp3", "title": "Song" },
    { "filename": "02 - Another.mp3", "title": "Another" }
  ],
  "total": 150,
  "queue_position": 3,
  "queue_total": 150
}
```

### `/title-stream` (SSE)

Each SSE `data` message is a JSON object:

```json
{
  "title": "Track Name",
  "queue": [
    { "title": "Track Name", "position": 1 },
    { "title": "Upcoming",   "position": 2 }
  ],
  "queue_position": 3,
  "queue_total": 150
}
```

Heartbeat messages (`: heartbeat\n\n`) are sent every second of inactivity to keep the connection alive through proxies.

## File Structure

```
radio-pipbuck/
├── radio.py              # Flask app + one broadcaster thread per station
├── requirements.txt      # Python dependencies
├── .env                  # Local configuration (not committed)
├── .env.example          # Configuration template
├── stations/             # Custom station configs (*.json, gitignored)
├── run.bat               # Windows launcher
├── Dockerfile
├── docker-compose.yml
├── README.md
└── templates/
    ├── stations.html     # Station selector (home page)
    ├── index.html        # Station player (full UI)
    ├── title.html        # Minimal "Now Playing" page
    ├── obs.html          # OBS Browser Source — transparent, audio-only
    └── pages.html        # All pages with descriptions (bilingual)
```

## Configuration

Tunable constants in `radio.py`:

| Constant              | Value     | Description                                                        |
|-----------------------|-----------|--------------------------------------------------------------------|
| `HOST`                | `0.0.0.0` | Bind address (all interfaces)                                      |
| `CHUNK`               | `4096`    | Bytes per read chunk. At 128 kbps ≈ 0.25 s per chunk              |
| `QUEUE_SIZE`          | `512`     | Max buffered chunks per client (~2 MB). Slow clients are dropped   |
| `PREBUFFER_MAX_BYTES` | `256000`  | Audio pre-buffer size (~10 s at 192 kbps, ~16 s at 128 kbps)      |

## localStorage

| Key             | Purpose                   |
|-----------------|---------------------------|
| `radio_volume`  | Volume preference (0–100) |

## OBS Browser Source Setup

Use `/<station_id>/obs` (or `/obs` for the general station) to capture the radio audio inside OBS without any visible UI.

1. In OBS, add a **Browser Source**
2. Set URL to `http://localhost:8000/obs` (or `http://localhost:8000/<station_id>/obs` for a custom station)
3. Width / Height can be as small as `1 × 1`
4. Make sure **"Shutdown source when not visible"** is **disabled**

The page is fully transparent and renders nothing. JavaScript maintains the stream with several independent recovery layers:

| Trigger | Action |
|---|---|
| `audio.error` | Reconnect after 2 s |
| `audio.ended` | Reconnect after 0.5 s |
| `currentTime` frozen for 8 s | Force reconnect |
| `play()` rejected | Retry after 2 s |
| SSE alive but audio silent | Reconnect after 0.5 s |

Each reconnect uses a cache-busted URL (`/<station_id>/stream?t=<timestamp>`) to bypass any browser or proxy cache.

## License

MIT
