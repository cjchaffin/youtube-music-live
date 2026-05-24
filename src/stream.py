import os
import sys
import time
import logging
import subprocess
import threading
import http.server
import shutil
from pathlib import Path
from typing import Optional, Dict, Any
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
            settings.DATA_PATH.mkdir(parents=True, exist_ok=True)
            with open(settings.DATA_PATH / "title.txt", "w", encoding="utf-8") as f:
                f.write(title)
            with open(settings.DATA_PATH / "artist.txt", "w", encoding="utf-8") as f:
                f.write(artist)
        except Exception as e:
            logger.error(f"Failed to write telemetry files: {e}")

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
                
                # Resolve escaped paths for FFmpeg filters depending on OS
                if os.name == 'nt':
                    f_path = str(settings.ASSETS_PATH / "bahnschrift.ttf").replace("\\", "/").replace(":", "\\:")
                    t_path = str(settings.DATA_PATH / "title.txt").replace("\\", "/").replace(":", "\\:")
                    a_path = str(settings.DATA_PATH / "artist.txt").replace("\\", "/").replace(":", "\\:")
                else:
                    f_path = "/app/assets/bahnschrift.ttf"
                    t_path = "/app/data/title.txt"
                    a_path = "/app/data/artist.txt"
                
                # drawtext overlays — positioned in the hero zone's track info panel
                # Canvas layout: track info x=620, title at y=244 (size 40), artist at y=320 (size 26)
                drawtext_chain = (
                    f",drawtext=fontfile='{f_path}':textfile='{t_path}':reload=1"
                    f":x=620:y=244:fontsize=40:fontcolor=white:shadowx=2:shadowy=2:shadowcolor=black"
                    f",drawtext=fontfile='{f_path}':textfile='{a_path}':reload=1"
                    f":x=620:y=320:fontsize=26:fontcolor=0x00F0FF:shadowx=1:shadowy=1:shadowcolor=black"
                )
                
                filter_complex = None

                # ── Visualizer overlay zone: x=60, y=660, 1800x300px (bottom strip) ──────────────
                if visualizer == "showfreqs":
                    # Frequency bar EQ — log scale, teal/violet bars per stereo channel
                    framerate = "30"
                    filter_complex = (
                        f"[1:a]showfreqs=s=900x150:mode=bar:ascale=sqrt:fscale=log"
                        f":colors=0x00CCFF|0x9944FF:win_func=hann"
                        f",scale=1800:300:flags=neighbor,format=yuv420p[freqs];"
                        f"[0:v][freqs]overlay=60:650{drawtext_chain}[outv]"
                    )
                elif visualizer == "showwaves":
                    # Centerline waveform — cyan and violet, full width
                    framerate = "25"
                    filter_complex = (
                        f"[1:a]showwaves=s=900x150:mode=cline:colors=0x00F0FFCC|0x8800FFCC"
                        f":scale=sqrt:draw=full,scale=1800:300:flags=neighbor,format=yuv420p[wave];"
                        f"[0:v][wave]overlay=60:650{drawtext_chain}[outv]"
                    )
                elif visualizer == "showcqt":
                    # Constant-Q transform spectrum — bars only, no piano labels
                    framerate = "30"
                    filter_complex = (
                        f"[1:a]showcqt=s=900x150:fps=30:bar_g=2:axis_h=0"
                        f":sono_h=0:bar_h=150:count=1:tc=0.33"
                        f":basefreq=20:endfreq=20000"
                        f",scale=1800:300:flags=neighbor,format=yuv420p[cqt];"
                        f"[0:v][cqt]overlay=60:650{drawtext_chain}[outv]"
                    )
                elif visualizer == "avectorscope":
                    # Lissajous vector scope — centered in the 1800px-wide viz strip
                    # Center offset: x=60+(1800-400)/2=760
                    framerate = "25"
                    filter_complex = (
                        f"[1:a]avectorscope=s=400x300:scale=lin:draw=dots"
                        f":rc=0:gc=240:bc=255:rf=50:gf=180:bf=255"
                        f",format=yuv420p[radar];"
                        f"[0:v][radar]overlay=760:650{drawtext_chain}[outv]"
                    )
                elif visualizer == "showspectrum":
                    # Scrolling spectrogram waterfall — full width
                    framerate = "25"
                    filter_complex = (
                        f"[1:a]showspectrum=s=900x150:slide=scroll"
                        f":color=channel:scale=cbrt:saturation=3"
                        f",scale=1800:300:flags=neighbor,format=yuv420p[spec];"
                        f"[0:v][spec]overlay=60:650{drawtext_chain}[outv]"
                    )
                else:  # "none" — text only, no visualizer
                    framerate = "5"
                    filter_complex = (
                        f"[0:v]"
                        f"drawtext=fontfile='{f_path}':textfile='{t_path}':reload=1"
                        f":x=620:y=244:fontsize=40:fontcolor=white:shadowx=2:shadowy=2:shadowcolor=black,"
                        f"drawtext=fontfile='{f_path}':textfile='{a_path}':reload=1"
                        f":x=620:y=320:fontsize=26:fontcolor=0x00F0FF:shadowx=1:shadowy=1:shadowcolor=black"
                        f"[outv]"
                    )

                ffmpeg_cmd = [
                    "ffmpeg",
                    "-y",
                    "-loop", "1",
                    "-framerate", framerate,
                    "-i", str(settings.canvas_static_path),
                    "-f", "s16le",
                    "-ac", "2",
                    "-ar", "44100",
                    "-i", "pipe:0"
                ]

                if filter_complex:
                    ffmpeg_cmd.extend(["-filter_complex", filter_complex])
                    ffmpeg_cmd.extend(["-map", "[outv]", "-map", "1:a"])
                else:
                    ffmpeg_cmd.extend(["-map", "0:v", "-map", "1:a"])

                ffmpeg_cmd.extend([
                    "-c:v", "libx264",
                    "-preset", "ultrafast",
                    "-tune", "stillimage" if not filter_complex else "film",
                    "-pix_fmt", "yuv420p",
                    "-g", "60" if filter_complex else "30",
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
