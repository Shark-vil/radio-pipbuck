# radio-pipbuck

Lightweight internet radio streaming server. Broadcasts MP3 files from a local directory to multiple clients simultaneously with real-time track metadata, playlist browsing, and a modern glassmorphic web player.

## Features

- **Continuous streaming** — granular chunk-based MP3 broadcasting with drift correction for accurate playback speed
- **Multi-client support** — serves unlimited concurrent listeners with per-client queue buffers and automatic slow-client detection
- **Smart queue management** — randomized shuffle playback; the next queue is pre-generated in the background before the current one ends, ensuring zero stream interruption
- **Real-time metadata** — Server-Sent Events (SSE) push live track titles and queue updates to all connected clients
- **Web player** — play/pause, volume slider with touch support, live status badge, listener count
- **Playlist panel** — browse all songs with search, view the current playback queue, all without interrupting the stream
- **Localization** — automatic language detection (Russian / English) for the entire UI
- **Cookie consent** — full-screen banner on first visit; volume is saved in cookies only after explicit consent
- **Minimal "Now Playing" page** — lightweight title display with AJAX polling for embeds or overlays
- **Docker-ready** — one-command deployment with `docker compose`

## Quick Start

### Prerequisites

- Python 3.12+
- A directory with `.mp3` files

### Environment Variables

| Variable    | Default        | Description                     |
|-------------|----------------|---------------------------------|
| `MUSIC_DIR` | `D:\mlpStream` | Path to directory with MP3 files|
| `PORT`      | `8000`         | HTTP server port                |

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

MUSIC_DIR=/path/to/music python radio.py
```

### Method 3 — Docker

```bash
mkdir music                       # place your .mp3 files here
docker compose up -d
```

The server is available at `http://localhost:8000`.

## API Endpoints

| Method | Path            | Description                            | Content-Type        |
|--------|-----------------|----------------------------------------|---------------------|
| GET    | `/`             | Main web player                        | `text/html`         |
| GET    | `/stream`       | Continuous audio stream                | `audio/mpeg`        |
| GET    | `/title`        | Current track title                    | `text/plain`        |
| GET    | `/title-page`   | Minimal "Now Playing" page (AJAX)      | `text/html`         |
| GET    | `/title-stream` | SSE: title + queue updates             | `text/event-stream` |
| GET    | `/status`       | Server status                          | `application/json`  |
| GET    | `/playlist`     | Full playlist and current queue        | `application/json`  |

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
    { "title": "Next Track",            "position": 2 },
    { "title": "After That",            "position": 3 }
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
├── radio.py              # Flask app + broadcaster thread
├── requirements.txt      # Python dependencies
├── run.bat               # Windows launcher
├── Dockerfile
├── docker-compose.yml
├── README.md
└── templates/
    ├── index.html        # Main player (full UI)
    └── title.html        # Minimal "Now Playing" page
```

## Configuration

Hardcoded constants in `radio.py`:

| Constant     | Value  | Description                                      |
|--------------|--------|--------------------------------------------------|
| `HOST`       | `0.0.0.0` | Bind address (all interfaces)                   |
| `CHUNK`      | `4096` | Bytes per read chunk. At 128 kbps ≈ 0.25s intervals |
| `QUEUE_SIZE` | `512`  | Max buffered chunks per client (~2 MB). Slow clients are dropped |

## Cookies

| Cookie            | Purpose                          | Lifetime |
|-------------------|----------------------------------|----------|
| `radio_volume`    | Volume preference (0–100)        | 365 days |
| `radio_consent`   | Consent choice (`0` or `1`)      | 365 days |

Volume is only stored if the user accepts the cookie consent banner.

## License

MIT
