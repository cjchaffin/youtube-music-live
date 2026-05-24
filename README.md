# YouTube Music Live Stream 🎵📺

A high-performance, containerized 24/7 livestreaming engine designed to stream YouTube Music playlists directly to RTMP ingest endpoints (e.g., YouTube Live) with dynamic audio visualizers and automated text-to-speech (TTS) host announcements.

Developed to run efficiently on home server environments (e.g., CasaOS, Docker, Linux/Windows hosts).

---

## Features

- **24/7 Livestreaming**: Automated music playback loop from YouTube Music playlist URLs, liked music catalog, or seed-based radio stations.
- **Dynamic Audio Visualizers**: Renders real-time audio visualization using FFmpeg filtergraphs (supports `showfreqs`, `showwaves`, `showcqt`, `showspectrum`, and `avectorscope`).
- **Interactive Host Breaks (TTS)**: Integrates a local Piper TTS voice engine to generate smooth, context-aware radio host announcements and inject them between tracks.
- **FastAPI Control Panel**: Responsive Web UI to monitor status, skip tracks, change visualizer modes, toggle TTS, adjust announcement frequency, and control the stream.
- **Performance Optimized**: Low CPU footprint (~130% CPU total on 12 threads) achieved via frame-rate throttling, in-memory telemetry caching, and optimized still-image encoding configurations.

---

## Project Structure

```
├── assets/                  # Static assets (fonts, background canvas)
├── config/                  # App configurations and auth keys
│   └── auth.json            # YouTube Music client credentials
├── data/                    # Dynamic cache (library database, TTS wave logs)
├── src/
│   ├── api.py               # FastAPI server and WebSocket logger
│   ├── config.py            # Global Settings (Pydantic)
│   ├── ingestion.py         # YouTube Music scraper & catalog fetcher
│   ├── orchestrator.py      # Main playlist dispatcher and TTS state machine
│   ├── stream.py            # FFmpeg encoding & transmission engine
│   └── tts.py               # Local Piper voice synthesizer client
├── web/                     # Dashboard Web UI assets (HTML/CSS/JS)
├── docker-compose.yml       # Production multi-container orchestration
├── Dockerfile               # Build configuration for python streaming environment
└── generate_canvas.py       # Pillow layout script to create static background frame
```

---

## Configuration

Configuration is managed via a `.env` file placed in the root directory:

```env
# Essential Keys
GEMINI_API_KEY=AIzaSy...              # Optional: For AI-generated host scripts
YOUTUBE_STREAM_KEY=xxxx-xxxx-xxxx-xxxx # Your YouTube Live RTMP stream key

# App Configurations
LOCAL_RTMP_TEST=False                 # True = stream to local RTMP container; False = stream to YouTube Live
STREAM_VISUALIZER=showfreqs            # Default visualizer (showfreqs, showcqt, showwaves, showspectrum, none)
PIPER_URL=http://piper:5000           # Piper TTS service connection URL
```

---

## Quick Start (Docker Compose)

The application runs inside a multi-container Docker Compose stack.

### 1. Build and Start
To build the custom stream container and run the stack:
```bash
docker compose up -d --build
```

### 2. Stream Destination
* **Production Mode** (`LOCAL_RTMP_TEST=False`): Automatically streams RTMP directly to YouTube Live: `rtmp://a.rtmp.youtube.com/live2/{YOUTUBE_STREAM_KEY}`
* **Testing Mode** (`LOCAL_RTMP_TEST=True`): Streams to the local Nginx RTMP container in the stack. Accessible at:
  ```
  rtmp://<host-ip>:1935/live/stream
  ```

### 3. Open the Dashboard
Access the Web UI controller dashboard at:
```
http://<host-ip>:8000/
```

---

## Tuning In (VLC Player Test)

To check the stream output locally:
1. Open **VLC Media Player**.
2. Press `Ctrl + N` (Open Network Stream).
3. Enter your test stream link:
   ```
   rtmp://<host-ip>:1935/live/stream
   ```
4. Click **Play** to start watching and listening.

---

## Performance Diagnostics & Optimization

Originally, computing high-resolution audio visualizations at 30 fps consumed significant CPU resources, leading to video hitching and frame drops on home servers. We optimized the FFmpeg pipeline on Linux to ensure a perfect `1.0x` real-time stream:

1. **In-Memory Caching (`/tmp`)**: Dynamic overlay files (`title.txt`, `artist.txt`) updated on every track change are written directly to `/tmp/` inside the container, avoiding bind-mount disk write latency.
2. **Pacing Sync (`-re`)**: Added `-re` on looped static video assets to force FFmpeg to read input at exactly the target framerate (25 fps), preventing unbounded CPU consumption.
3. **Encoder Profiles**: Restructured H.264 parameters (`-preset ultrafast`, `-tune stillimage`, and `-threads 4`) to minimize encoding overhead on static elements.

### Benchmark Results (NAS Host: Intel i7-8700K 12 Threads)

| Metric | Unoptimized (30 FPS, film tune, bind-mount I/O) | Optimized (25 FPS, stillimage tune, `/tmp` cache) |
| :--- | :--- | :--- |
| **CPU Usage** | **`392%`** (Saturated cores) | **`132%`** (1.3 CPU cores) |
| **Speed Ratio** | `0.68x - 0.91x` (Lagging/Hitching) | `0.994x - 1.00x` (Smooth Real-time) |
| **Disk I/O** | 60 reads/sec on host disk mount | Negligible (virtual memory directory) |
