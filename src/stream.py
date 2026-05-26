import os
import sys
import time
import logging
import subprocess
import threading
import http.server
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import yt_dlp

from src.config import settings
from src.orchestrator import orchestrator

logger = logging.getLogger("stream")

class StreamEngine:
    def __init__(self):
        self.stream_thread: Optional[threading.Thread] = None
        self.transmitter_proc: Optional[subprocess.Popen] = None
        self.decoder_proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._hls_server: Optional[http.server.HTTPServer] = None
        self._hls_server_thread: Optional[threading.Thread] = None
        self._clock_thread: Optional[threading.Thread] = None
        
    def resolve_stream_url(self, video_id: str) -> Optional[tuple]:
        """
        Uses yt-dlp to extract the direct audio stream URL and HTTP headers from YouTube Music.
        """
        url = f"https://music.youtube.com/watch?v={video_id}"
        orchestrator.log(f"Resolving streaming audio URL for track {video_id}...", "system")
        
        ydl_opts = {
            'format': 'bestaudio/best',
            'quiet': True,
            'no_warnings': True,
            'nocheckcertificate': True,
            'ignoreerrors': True,
        }
        
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if info:
                    stream_url = info.get('url')
                    headers = info.get('http_headers') or {}
                    if stream_url:
                        headers_str = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
                        return stream_url, headers_str
            orchestrator.log(f"Failed to find stream URL in metadata for video: {video_id}", "error")
        except Exception as e:
            orchestrator.log(f"yt-dlp extraction error for {video_id}: {e}", "error")
            
        return None
        
    def _write_telemetry_files(self, title: str, artist: str):
        """Writes current track metadata to text files for FFmpeg drawtext overlay."""
        try:
            if os.name == 'nt':
                settings.DATA_PATH.mkdir(parents=True, exist_ok=True)
                t_path = settings.DATA_PATH / "title.txt"
                a_path = settings.DATA_PATH / "artist.txt"
            else:
                t_path = Path("/tmp/title.txt")
                a_path = Path("/tmp/artist.txt")
            with open(t_path, "w", encoding="utf-8") as f:
                f.write(title)
            with open(a_path, "w", encoding="utf-8") as f:
                f.write(artist)
        except Exception as e:
            logger.error(f"Failed to write telemetry files: {e}")

    def _clock_file_path(self) -> Path:
        if os.name == 'nt':
            settings.DATA_PATH.mkdir(parents=True, exist_ok=True)
            return settings.DATA_PATH / "clock.txt"
        return Path("/tmp/clock.txt")

    def _central_clock_text(self) -> str:
        try:
            now = datetime.now(ZoneInfo("America/Chicago"))
        except ZoneInfoNotFoundError:
            now = datetime.now()
        return now.strftime("%I:%M %p").lstrip("0")

    def _start_clock_writer(self):
        """Updates a tiny text file so FFmpeg can render a live clock."""
        if self._clock_thread and self._clock_thread.is_alive():
            return

        def write_loop():
            path = self._clock_file_path()
            while orchestrator.is_running:
                try:
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(self._central_clock_text())
                except Exception as e:
                    logger.warning(f"Failed to write clock telemetry: {e}")
                time.sleep(1)

        self._clock_thread = threading.Thread(
            target=write_loop,
            name="ClockTelemetryWriter",
            daemon=True,
        )
        self._clock_thread.start()

    def start_stream_loop(self):
        """
        Main stream orchestration loop. Spawns FFmpeg transmitter and pipes audio streams to it.
        """
        orchestrator.log("Starting master A/V transmitter loop...", "system")
        
        # Verify canvas exists
        if not settings.canvas_static_path.exists():
            orchestrator.log(f"ERROR: Canvas image not found at {settings.canvas_static_path}. Stream cannot start.", "error")
            orchestrator.is_running = False
            return

        self._write_telemetry_files("Connecting...", "Please wait")

        try:
            while orchestrator.is_running:
                # 1. Determine output endpoint (YouTube RTMP, local RTMP server, or local test file)
                rtmp_url = ""
                is_rtmp = False
                if settings.YOUTUBE_STREAM_KEY:
                    out_url = f"rtmp://a.rtmp.youtube.com/live2/{settings.YOUTUBE_STREAM_KEY}"
                    out_format = "flv"
                    orchestrator.log("Streaming destination: YOUTUBE LIVE RTMP SERVERS", "system")
                elif settings.LOCAL_RTMP_TEST:
                    # Use rtmp://rtmp:1935 when running in Docker Compose,
                    # fall back to HLS on localhost if the rtmp hostname isn't reachable
                    import socket
                    rtmp_host = "rtmp"
                    try:
                        socket.setdefaulttimeout(1)
                        socket.getaddrinfo(rtmp_host, 1935)
                        # Docker RTMP service is reachable
                        out_url = f"rtmp://{rtmp_host}:1935/live/stream"
                        out_format = "flv"
                        orchestrator.log(f"Streaming destination: LOCAL RTMP (Docker)  →  {out_url}", "system")
                        orchestrator.log("VLC: open rtmp://localhost:1935/live/stream", "system")
                    except Exception:
                        # Not in Docker — fall back to local HLS served over HTTP
                        hls_dir = settings.DATA_PATH / "hls"
                        hls_dir.mkdir(parents=True, exist_ok=True)
                        for f in hls_dir.glob("*.ts"):
                            f.unlink(missing_ok=True)
                        for f in hls_dir.glob("*.m3u8"):
                            f.unlink(missing_ok=True)
                        out_url = str(hls_dir / "stream.m3u8")
                        out_format = "hls"
                        orchestrator.log("RTMP server not reachable. Using local HLS fallback.", "warning")
                        orchestrator.log("VLC: open http://localhost:8888/stream.m3u8", "system")
                        self._start_hls_http_server(hls_dir)
                else:
                    out_url = str(settings.PROJECT_ROOT / "test_out.mp4")
                    out_format = "mp4"
                    orchestrator.log(f"Streaming destination: LOCAL FILE ({out_url})", "system")

                # 2. Spawn the persistent FFmpeg transmitter process
                # -y overwrites local file if dry-running
                # Inputs: static image (looping) + stdin raw PCM s16le audio
                # -re is applied before the video loop input and before the audio pipe input
                # to guarantee stable real-time playback.
                
                visualizer = settings.STREAM_VISUALIZER.lower()
                framerate = "2"
                background_video_path = settings.ASSETS_PATH / "workshop-base.png"
                use_video_background = background_video_path.exists()
                self._start_clock_writer()
                
                # Resolve escaped paths for FFmpeg filters depending on OS
                if os.name == 'nt':
                    f_path = str(settings.ASSETS_PATH / "bahnschrift.ttf").replace("\\", "/").replace(":", "\\:")
                    t_path = str(settings.DATA_PATH / "title.txt").replace("\\", "/").replace(":", "\\:")
                    a_path = str(settings.DATA_PATH / "artist.txt").replace("\\", "/").replace(":", "\\:")
                    c_path = str(self._clock_file_path()).replace("\\", "/").replace(":", "\\:")
                else:
                    f_path = "/app/assets/bahnschrift.ttf"
                    t_path = "/tmp/title.txt"
                    a_path = "/tmp/artist.txt"
                    c_path = "/tmp/clock.txt"
                
                # Drawtext overlays positioned inside the canvas metadata panel.
                drawtext_chain = (
                    f",drawtext=fontfile='{f_path}':textfile='{t_path}':reload=1"
                    f":x=680:y=266:fontsize=46:fontcolor=white:shadowx=3:shadowy=3:shadowcolor=black"
                    f",drawtext=fontfile='{f_path}':textfile='{a_path}':reload=1"
                    f":x=680:y=350:fontsize=30:fontcolor=0x00F0FF:shadowx=2:shadowy=2:shadowcolor=black"
                )
                
                filter_complex = None

                # Visualizer overlay zone: x=60, y=716, 1800x268px (bottom strip).
                if visualizer == "showfreqs":
                    # Frequency bar EQ — log scale, teal/violet bars per stereo channel
                    framerate = "25"
                    filter_complex = (
                        f"[1:a]showfreqs=s=1800x268:mode=bar:ascale=sqrt:fscale=log"
                        f":colors=cyan|magenta:win_func=hann"
                        f",format=rgba,colorkey=0x000000:0.10:0.05[freqs];"
                        f"[0:v][freqs]overlay=60:716{drawtext_chain}[outv]"
                    )
                elif visualizer == "showwaves":
                    # Centerline waveform — cyan and violet, full width
                    framerate = "25"
                    filter_complex = (
                        f"[1:a]showwaves=s=1800x268:mode=cline:colors=cyan|magenta"
                        f":scale=sqrt:draw=full,format=rgba,colorkey=0x000000:0.10:0.05[wave];"
                        f"[0:v][wave]overlay=60:716{drawtext_chain}[outv]"
                    )
                elif visualizer == "showcqt":
                    # Constant-Q transform spectrum — bars only, no piano labels
                    framerate = "25"
                    filter_complex = (
                        f"[1:a]showcqt=s=1800x268:fps=25:bar_g=2:axis_h=0"
                        f":sono_h=0:bar_h=268:count=1:tc=0.33"
                        f":basefreq=20:endfreq=20000"
                        f",format=rgba,colorkey=0x000000:0.10:0.05[cqt];"
                        f"[0:v][cqt]overlay=60:716{drawtext_chain}[outv]"
                    )
                elif visualizer == "avectorscope":
                    # Lissajous vector scope — centered in the 1800px-wide viz strip
                    # Center offset: x=60+(1800-420)/2=750
                    framerate = "25"
                    filter_complex = (
                        f"[1:a]avectorscope=s=420x268:scale=lin:draw=dots"
                        f":rc=255:gc=49:bc=150:rf=0:gf=240:bf=255"
                        f",format=rgba,colorkey=0x000000:0.10:0.05[radar];"
                        f"[0:v][radar]overlay=750:716{drawtext_chain}[outv]"
                    )
                elif visualizer == "showspectrum":
                    # Scrolling spectrogram waterfall — full width
                    framerate = "25"
                    filter_complex = (
                        f"[1:a]showspectrum=s=1800x268:slide=scroll"
                        f":color=channel:scale=cbrt:saturation=4"
                        f",format=rgba,colorkey=0x000000:0.10:0.05[spec];"
                        f"[0:v][spec]overlay=60:716{drawtext_chain}[outv]"
                    )
                else:  # "none" — text only, no visualizer
                    framerate = "5"
                    filter_complex = (
                        f"[0:v]"
                        f"drawtext=fontfile='{f_path}':textfile='{t_path}':reload=1"
                        f":x=680:y=266:fontsize=46:fontcolor=white:shadowx=3:shadowy=3:shadowcolor=black,"
                        f"drawtext=fontfile='{f_path}':textfile='{a_path}':reload=1"
                        f":x=680:y=350:fontsize=30:fontcolor=0x00F0FF:shadowx=2:shadowy=2:shadowcolor=black"
                        f"[outv]"
                    )

                if use_video_background:
                    framerate = "25"
                    def monitor_surface(label, x0, y0, x1, y1, x2, y2, x3, y3):
                        left = min(x0, x1, x2, x3)
                        top = min(y0, y1, y2, y3)
                        right = max(x0, x1, x2, x3)
                        bottom = max(y0, y1, y2, y3)
                        rel = (
                            x0 - left, y0 - top,
                            x1 - left, y1 - top,
                            x2 - left, y2 - top,
                            x3 - left, y3 - top,
                        )
                        return {
                            "label": label,
                            "x": left,
                            "y": top,
                            "w": right - left,
                            "h": bottom - top,
                            "perspective": (
                                f"perspective=x0={rel[0]}:y0={rel[1]}:x1={rel[2]}:y1={rel[3]}"
                                f":x2={rel[4]}:y2={rel[5]}:x3={rel[6]}:y3={rel[7]}"
                                f":sense=destination:eval=init"
                            ),
                        }

                    # Fixed screen canvases for the cropped workshop image.
                    # Corner order is top-left, top-right, bottom-left, bottom-right.
                    monitors = {
                        "left": monitor_surface("left", 654, 301, 811, 323, 680, 554, 808, 541),
                        "center": monitor_surface("center", 816, 387, 1149, 392, 816, 577, 1144, 579),
                        "right": monitor_surface("right", 1331, 382, 1534, 338, 1331, 506, 1530, 545),
                    }
                    left = monitors["left"]
                    center = monitors["center"]
                    right = monitors["right"]
                    center_safe_x = 28
                    center_safe_y = 14
                    center_safe_w = center["w"] - 46
                    center_safe_h = center["h"] - 36
                    room_base_chain = (
                        f"[0:v]scale=1920:1080,crop=1728:972:96:24,scale=1920:1080,setsar=1,fps=25[room];"
                        f"color=c=black@0.0:s={left['w']}x{left['h']}:r=25,format=rgba,"
                        f"drawtext=fontfile='{f_path}':textfile='{c_path}':reload=1"
                        f":x=10:y=20:fontsize=22:fontcolor=0xBFE8FF"
                        f":shadowx=2:shadowy=2:shadowcolor=black,"
                        f"drawtext=fontfile='{f_path}':textfile='{t_path}':reload=1"
                        f":x=10:y=74:fontsize=11:fontcolor=0xF4E6FF"
                        f":shadowx=2:shadowy=2:shadowcolor=black,"
                        f"drawtext=fontfile='{f_path}':textfile='{a_path}':reload=1"
                        f":x=10:y=102:fontsize=11:fontcolor=0xBFE8FF"
                        f":shadowx=2:shadowy=2:shadowcolor=black,{left['perspective']}[left_canvas];"
                        f"color=c=black@0.0:s={right['w']}x{right['h']}:r=25,format=rgba,"
                        f"drawtext=fontfile='{f_path}':text='LIVE'"
                        f":x=18:y=24:fontsize=25:fontcolor=0xF4E6FF"
                        f":shadowx=2:shadowy=2:shadowcolor=black,"
                        f"drawtext=fontfile='{f_path}':text='AUDIO REACTIVE'"
                        f":x=18:y=70:fontsize=14:fontcolor=0x9FE7FF"
                        f":shadowx=2:shadowy=2:shadowcolor=black,{right['perspective']}[right_canvas];"
                    )
                    if visualizer == "showwaves":
                        filter_complex = (
                            f"{room_base_chain}[1:a]showwaves=s={center_safe_w}x{center_safe_h}:mode=cline:colors=cyan|magenta"
                            f":scale=sqrt:draw=full,format=rgba,colorkey=0x000000:0.10:0.05,"
                            f"colorchannelmixer=aa=0.85,"
                            f"pad={center['w']}:{center['h']}:{center_safe_x}:{center_safe_y}:color=black@0.0,"
                            f"{center['perspective']}[center_canvas];"
                            f"[room][left_canvas]overlay={left['x']}:{left['y']}[lefted];"
                            f"[lefted][center_canvas]overlay={center['x']}:{center['y']}[centered];"
                            f"[centered][right_canvas]overlay={right['x']}:{right['y']}[outv]"
                        )
                    elif visualizer == "showcqt":
                        filter_complex = (
                            f"{room_base_chain}[1:a]showcqt=s={center_safe_w}x{center_safe_h}:fps=25:bar_g=2:axis_h=0"
                            f":sono_h=0:bar_h={center_safe_h}:count=1:tc=0.33:basefreq=20:endfreq=20000"
                            f",format=rgba,colorkey=0x000000:0.10:0.05,colorchannelmixer=aa=0.85,"
                            f"pad={center['w']}:{center['h']}:{center_safe_x}:{center_safe_y}:color=black@0.0,"
                            f"{center['perspective']}[center_canvas];"
                            f"[room][left_canvas]overlay={left['x']}:{left['y']}[lefted];"
                            f"[lefted][center_canvas]overlay={center['x']}:{center['y']}[centered];"
                            f"[centered][right_canvas]overlay={right['x']}:{right['y']}[outv]"
                        )
                    elif visualizer == "avectorscope":
                        scope_width = 150
                        filter_complex = (
                            f"{room_base_chain}[1:a]avectorscope=s={scope_width}x{center_safe_h}:scale=lin:draw=dots"
                            f":rc=255:gc=49:bc=150:rf=0:gf=240:bf=255"
                            f",format=rgba,colorkey=0x000000:0.10:0.05,colorchannelmixer=aa=0.85,"
                            f"pad={center_safe_w}:{center_safe_h}:{(center_safe_w - scope_width) // 2}:0:color=black@0.0,"
                            f"pad={center['w']}:{center['h']}:{center_safe_x}:{center_safe_y}:color=black@0.0,"
                            f"{center['perspective']}[center_canvas];"
                            f"[room][left_canvas]overlay={left['x']}:{left['y']}[lefted];"
                            f"[lefted][center_canvas]overlay={center['x']}:{center['y']}[centered];"
                            f"[centered][right_canvas]overlay={right['x']}:{right['y']}[outv]"
                        )
                    elif visualizer == "showspectrum":
                        filter_complex = (
                            f"{room_base_chain}[1:a]showspectrum=s={center_safe_w}x{center_safe_h}:slide=scroll"
                            f":color=channel:scale=cbrt:saturation=4"
                            f",format=rgba,colorkey=0x000000:0.10:0.05,colorchannelmixer=aa=0.85,"
                            f"pad={center['w']}:{center['h']}:{center_safe_x}:{center_safe_y}:color=black@0.0,"
                            f"{center['perspective']}[center_canvas];"
                            f"[room][left_canvas]overlay={left['x']}:{left['y']}[lefted];"
                            f"[lefted][center_canvas]overlay={center['x']}:{center['y']}[centered];"
                            f"[centered][right_canvas]overlay={right['x']}:{right['y']}[outv]"
                        )
                    elif visualizer == "none":
                        filter_complex = (
                            f"{room_base_chain}"
                            f"color=c=black@0.0:s={center['w']}x{center['h']}:r=25,format=rgba,"
                            f"drawtext=fontfile='{f_path}':text='LOFI LIVE'"
                            f":x=34:y=48:fontsize=40:fontcolor=0xF4E6FF"
                            f":shadowx=3:shadowy=3:shadowcolor=black,{center['perspective']}[center_canvas];"
                            f"[room][left_canvas]overlay={left['x']}:{left['y']}[lefted];"
                            f"[lefted][center_canvas]overlay={center['x']}:{center['y']}[centered];"
                            f"[centered][right_canvas]overlay={right['x']}:{right['y']}[outv]"
                        )
                    else:
                        filter_complex = (
                            f"{room_base_chain}[1:a]showfreqs=s={center_safe_w}x{center_safe_h}:mode=bar:ascale=sqrt:fscale=log"
                            f":colors=cyan|magenta:win_func=hann"
                            f",format=rgba,colorkey=0x000000:0.10:0.05,colorchannelmixer=aa=0.85,"
                            f"pad={center['w']}:{center['h']}:{center_safe_x}:{center_safe_y}:color=black@0.0,"
                            f"{center['perspective']}[center_canvas];"
                            f"[room][left_canvas]overlay={left['x']}:{left['y']}[lefted];"
                            f"[lefted][center_canvas]overlay={center['x']}:{center['y']}[centered];"
                            f"[centered][right_canvas]overlay={right['x']}:{right['y']}[outv]"
                        )

                ffmpeg_cmd = ["ffmpeg", "-y"]
                if os.name != 'nt':
                    ffmpeg_cmd.append("-re")
                if use_video_background:
                    if background_video_path.suffix.lower() in [".png", ".jpg", ".jpeg"]:
                        ffmpeg_cmd.extend([
                            "-loop", "1",
                            "-framerate", framerate,
                            "-i", str(background_video_path),
                        ])
                    else:
                        ffmpeg_cmd.extend([
                            "-stream_loop", "-1",
                            "-i", str(background_video_path),
                        ])
                else:
                    ffmpeg_cmd.extend([
                        "-loop", "1",
                        "-framerate", framerate,
                        "-i", str(settings.canvas_static_path),
                    ])
                ffmpeg_cmd.extend([
                    "-f", "s16le",
                    "-ac", "2",
                    "-ar", "44100",
                    "-i", "pipe:0"
                ])

                if filter_complex:
                    ffmpeg_cmd.extend(["-filter_complex", filter_complex])
                    ffmpeg_cmd.extend(["-map", "[outv]", "-map", "1:a"])
                else:
                    ffmpeg_cmd.extend(["-map", "0:v", "-map", "1:a"])

                ffmpeg_cmd.extend([
                    "-c:v", "libx264",
                    "-preset", "ultrafast",
                    "-threads", "4",
                    "-pix_fmt", "yuv420p",
                    "-g", str(int(framerate) * 2) if filter_complex else "30",
                    "-c:a", "aac",
                    "-b:a", "192k",
                    "-ar", "44100",
                ])

                if out_format == "hls":
                    ffmpeg_cmd.extend([
                        "-f", "hls",
                        "-hls_time", "2",
                        "-hls_list_size", "10",
                        "-hls_flags", "delete_segments+append_list",
                        "-hls_segment_filename", str(settings.DATA_PATH / "hls" / "seg%05d.ts"),
                        out_url
                    ])
                else:
                    ffmpeg_cmd.extend(["-f", out_format, out_url])

                try:
                    # Spawn transmitter (direct stdout to DEVNULL to prevent pipe buffer deadlock)
                    self.transmitter_proc = subprocess.Popen(
                        ffmpeg_cmd,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE,
                        bufsize=0
                    )
                    orchestrator.log("FFmpeg A/V transmitter process initialized successfully.", "success")
                    
                    # Monitor transmitter errors in a separate logging thread
                    threading.Thread(
                        target=self._log_ffmpeg_stderr, 
                        args=(self.transmitter_proc,), 
                        daemon=True
                    ).start()

                except Exception as e:
                    orchestrator.log(f"Failed to start FFmpeg transmitter: {e}", "error")
                    orchestrator.is_running = False
                    break

                # 3. Stream tracks loop
                while orchestrator.is_running:
                    # Check if transmitter has crashed
                    if not self.transmitter_proc or self.transmitter_proc.poll() is not None:
                        orchestrator.log("A/V transmitter process terminated unexpectedly. Restarting...", "error")
                        break

                    # Get next track (check for host breaks first)
                    track = orchestrator.get_voiceover_track()
                    if not track:
                        track = orchestrator.get_next_track()

                    if not track:
                        orchestrator.log("No tracks available in queue. Waiting...", "system")
                        time.sleep(3)
                        continue

                    # Set track details
                    with orchestrator._lock:
                        orchestrator.current_track = track
                    if orchestrator.on_track_change:
                        orchestrator.on_track_change(track)

                    # Update dynamic telemetry text files
                    title = track.get("title", "Unknown Title")
                    artist = track.get("artist", "Unknown Artist")
                    if track.get("is_tts"):
                        title = track.get("title", "Host Break")
                        artist = "ANTIGRAVITY TTS"
                    
                    if len(title) > 32:
                        title = title[:29] + "..."
                    if len(artist) > 32:
                        artist = artist[:29] + "..."
                        
                    self._write_telemetry_files(title, artist)

                    # Resolve stream path / url and headers
                    source_path = ""
                    headers_str = ""
                    if track.get("is_tts"):
                        # Local synthesis audio path
                        source_path = track.get("path")
                        orchestrator.log(f"Injecting host break audio: {track.get('title')}", "system")
                    elif track.get("path"):
                        # Local test/cache audio path, useful for visual QA without YouTube auth.
                        source_path = track.get("path")
                        orchestrator.log(f"Broadcasting local test audio: '{track.get('title')}' by {track.get('artist')}", "system")
                    else:
                        # YTMusic stream URL resolution
                        res = self.resolve_stream_url(track.get("track_id"))
                        if not res:
                            orchestrator.log(f"Could not resolve track: {track.get('title')}. Skipping.", "error")
                            continue
                        source_path, headers_str = res
                        orchestrator.log(f"Broadcasting: '{track.get('title')}' by {track.get('artist')}", "system")

                    # Spawn decoder subprocess to decode media stream to raw PCM s16le
                    # Silence verbose outputs using -loglevel error and -nostats to prevent pipe buffer issues.
                    # Inject reconnect parameters and resolved HTTP headers to bypass YouTube rate-limiting.
                    decode_cmd = ["ffmpeg", "-loglevel", "error", "-nostats"]
                    if source_path.startswith("http"):
                        decode_cmd.extend([
                            "-tls_verify", "0",
                            "-reconnect", "1",
                            "-reconnect_streamed", "1",
                            "-reconnect_delay_max", "5"
                        ])
                        if headers_str:
                            decode_cmd.extend(["-headers", headers_str])
                    decode_cmd.extend([
                        "-i", source_path,
                        "-f", "s16le",
                        "-ac", "2",
                        "-ar", "44100",
                        "pipe:1"
                    ])

                    try:
                        self.decoder_proc = subprocess.Popen(
                            decode_cmd,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL
                        )
                    except Exception as e:
                        orchestrator.log(f"Failed to initialize audio decoder: {e}", "error")
                        continue

                    # Write chunks to transmitter stdin
                    bytes_written = 0
                    chunk_size = 4096  # 4KB chunks for low latency skipping
                    write_error = False
                    
                    track_start_time = time.time()

                    while orchestrator.is_running:
                        # Check skip button command
                        if orchestrator.skip_requested:
                            orchestrator.log("Skip signal received. Terminating current track...", "system")
                            orchestrator.skip_requested = False
                            break

                        # Check if transmitter is alive
                        if not self.transmitter_proc or self.transmitter_proc.poll() is not None:
                            orchestrator.log("Transmitter terminated during decoding. Aborting track.", "error")
                            write_error = True
                            break

                        # Read PCM chunk from decoder
                        chunk = self.decoder_proc.stdout.read(chunk_size)
                        if not chunk:
                            break  # Track finished decoding naturally

                        try:
                            if not self.transmitter_proc:
                                raise ValueError("Transmitter process is None")
                            self.transmitter_proc.stdin.write(chunk)
                            bytes_written += len(chunk)

                            # Throttle writing to real-time speed: s16le (2ch, 2bytes, 44100Hz) = 176400 bytes/sec
                            expected_elapsed = bytes_written / 176400
                            actual_elapsed = time.time() - track_start_time
                            sleep_time = expected_elapsed - actual_elapsed
                            if sleep_time > 0.005:  # Sleep only if we are ahead by more than 5ms
                                time.sleep(sleep_time)

                            # Calculate elapsed seconds: s16le (2ch, 2bytes, 44100Hz) = 176400 bytes/sec
                            elapsed = bytes_written / 176400
                            if int(elapsed) > int((bytes_written - len(chunk)) / 176400):
                                if orchestrator.on_progress:
                                    orchestrator.on_progress(int(elapsed), track.get("duration_seconds", 0))

                        except (OSError, ValueError, AttributeError) as e:
                            orchestrator.log(f"Pipe write error (broken stream): {e}", "error")
                            write_error = True
                            break

                    # Clean up decoder process cleanly with timeout safety
                    try:
                        self.decoder_proc.terminate()
                        try:
                            self.decoder_proc.communicate(timeout=2)
                        except subprocess.TimeoutExpired:
                            logger.warning("Decoder did not terminate in time. Killing...")
                            self.decoder_proc.kill()
                            self.decoder_proc.communicate()
                        
                        if self.decoder_proc.returncode is not None and self.decoder_proc.returncode != 0:
                            orchestrator.log(f"Audio decoder exited with non-zero code: {self.decoder_proc.returncode}", "error")
                    except Exception as e:
                        logger.warning(f"Exception cleaning up decoder: {e}")
                    self.decoder_proc = None

                    # Clean up active track telemetry
                    with orchestrator._lock:
                        orchestrator.current_track = None
                    if orchestrator.on_track_change:
                        orchestrator.on_track_change(None)

                    if write_error:
                        break  # Break out of tracks loop to restart transmitter

                # Terminate transmitter on break
                self.terminate_transmitter()
        finally:
            # Absolute cleanup guarantee
            self._write_telemetry_files("Stream Offline", "")
            self.terminate_transmitter()
            if self.decoder_proc:
                try:
                    self.decoder_proc.terminate()
                    try:
                        self.decoder_proc.communicate(timeout=2)
                    except subprocess.TimeoutExpired:
                        self.decoder_proc.kill()
                        self.decoder_proc.communicate()
                except Exception:
                    pass
                self.decoder_proc = None
            orchestrator.log("Master A/V transmitter loop stopped.", "system")

    def _log_ffmpeg_stderr(self, proc):
        """Reads FFmpeg transmitter logs line-by-line and feeds them to the console."""
        buffer = []
        try:
            while True:
                # Read 1 byte at a time to handle carriage returns (\r) and newlines (\n)
                char_bytes = proc.stderr.read(1)
                if not char_bytes:
                    break
                try:
                    char = char_bytes.decode('utf-8', errors='ignore')
                except Exception:
                    continue
                if char in ('\r', '\n'):
                    if buffer:
                        line_str = "".join(buffer).strip()
                        buffer.clear()
                        if line_str:
                            if "frame=" in line_str or "fps=" in line_str or "bitrate=" in line_str:
                                orchestrator.log(f"[FFmpeg] {line_str}", "ffmpeg")
                            else:
                                orchestrator.log(f"[FFmpeg Log] {line_str}", "system")
                else:
                    buffer.append(char)
        except Exception as e:
            logger.warning(f"Error reading FFmpeg stderr: {e}")

    def _start_hls_http_server(self, hls_dir: Path):
        """Starts a simple HTTP server on port 8888 serving the HLS directory.
        Idempotent — if already running, does nothing.
        """
        if self._hls_server is not None:
            return  # Already running
        try:
            import functools
            handler = functools.partial(
                http.server.SimpleHTTPRequestHandler,
                directory=str(hls_dir)
            )
            self._hls_server = http.server.HTTPServer(("", 8888), handler)
            self._hls_server_thread = threading.Thread(
                target=self._hls_server.serve_forever,
                name="HLSHttpServer",
                daemon=True,
            )
            self._hls_server_thread.start()
            orchestrator.log("HLS HTTP server started on http://localhost:8888/stream.m3u8", "success")
        except Exception as e:
            logger.error(f"Failed to start HLS HTTP server: {e}")

    def _stop_hls_http_server(self):
        """Shuts down the HLS HTTP server if running."""
        if self._hls_server:
            try:
                self._hls_server.shutdown()
            except Exception:
                pass
            self._hls_server = None
            self._hls_server_thread = None
            orchestrator.log("HLS HTTP server stopped.", "system")

    def terminate_transmitter(self):
        """Safely shuts down the FFmpeg transmitter."""
        if self.transmitter_proc:
            orchestrator.log("Terminating persistent FFmpeg transmitter process...", "system")
            try:
                self.transmitter_proc.stdin.close()
            except Exception:
                pass
            try:
                self.transmitter_proc.terminate()
                try:
                    self.transmitter_proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    logger.warning("Transmitter did not terminate in time. Killing...")
                    self.transmitter_proc.kill()
                    self.transmitter_proc.wait()
            except Exception as e:
                logger.warning(f"Exception terminating transmitter: {e}")
            self.transmitter_proc = None

    def start(self):
        with self._lock:
            if self.stream_thread and self.stream_thread.is_alive():
                logger.warning("Stream loop is already running.")
                return
            
            orchestrator.is_running = True
            self.stream_thread = threading.Thread(target=self.start_stream_loop, name="MasterStreamThread", daemon=True)
            self.stream_thread.start()

    def stop(self):
        with self._lock:
            orchestrator.is_running = False
            orchestrator.log("Shutdown signal broadcasted to stream engine.", "system")
            
        # Terminate active sub-decoders to break loops immediately
        if self.decoder_proc:
            try:
                self.decoder_proc.terminate()
            except Exception:
                pass
        
        self.terminate_transmitter()
        self._stop_hls_http_server()

    def change_visualizer(self, visualizer: str):
        """
        Dynamically changes the active stream visualizer.
        Terminates the current transmitter process so it automatically restarts
        with the new FFmpeg visualizer configuration on the next track loop iteration.
        """
        orchestrator.log(f"Changing visualizer to: '{visualizer}'", "system")
        with self._lock:
            settings.STREAM_VISUALIZER = visualizer
            
        # Terminate active transmitter, triggering automatic restart in start_stream_loop
        self.terminate_transmitter()

# Create engine singleton
engine = StreamEngine()
