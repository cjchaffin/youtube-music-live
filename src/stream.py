import os
import sys
import time
import logging
import subprocess
import threading
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
        
    def resolve_stream_url(self, video_id: str) -> Optional[str]:
        """
        Uses yt-dlp to extract the direct audio stream URL from YouTube Music.
        """
        url = f"https://music.youtube.com/watch?v={video_id}"
        orchestrator.log(f"Resolving streaming audio URL for track {video_id}...", "system")
        
        ydl_opts = {
            'format': 'bestaudio/best',
            'quiet': True,
            'no_warnings': True,
            'nocheckcertificate': True,
            'ignoreerrors': True,
            'extractor_args': {
                'youtube': {
                    'player_client': ['web', 'ios']
                }
            }
        }
        
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if info:
                    stream_url = info.get('url')
                    if stream_url:
                        return stream_url
            orchestrator.log(f"Failed to find stream URL in metadata for video: {video_id}", "error")
        except Exception as e:
            orchestrator.log(f"yt-dlp extraction error for {video_id}: {e}", "error")
            
        return None

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

        while orchestrator.is_running:
            # 1. Determine output endpoint (YouTube RTMP or local test file)
            rtmp_url = ""
            if settings.YOUTUBE_STREAM_KEY:
                rtmp_url = f"rtmp://a.rtmp.youtube.com/live2/{settings.YOUTUBE_STREAM_KEY}"
                orchestrator.log("Streaming destination: YOUTUBE LIVE RTMP SERVERS", "system")
            else:
                rtmp_url = str(settings.PROJECT_ROOT / "test_out.mp4")
                orchestrator.log(f"Streaming destination: LOCAL SIMULATOR ({rtmp_url})", "system")

            # 2. Spawn the persistent FFmpeg transmitter process
            # -y overwrites local file if dry-running
            # Inputs: static image (looping) + stdin raw PCM s16le audio
            ffmpeg_cmd = [
                "ffmpeg",
                "-y",
                "-loop", "1",
                "-framerate", "2",
                "-i", str(settings.canvas_static_path),
                "-f", "s16le",
                "-ac", "2",
                "-ar", "44100",
                "-i", "pipe:0",
                "-map", "0:v",
                "-map", "1:a",
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-tune", "stillimage",
                "-pix_fmt", "yuv420p",
                "-g", "4",
                "-c:a", "aac",
                "-b:a", "192k",
                "-ar", "44100",
                "-f", "flv" if settings.YOUTUBE_STREAM_KEY else "mp4",
                rtmp_url
            ]

            try:
                # Spawn transmitter
                self.transmitter_proc = subprocess.Popen(
                    ffmpeg_cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
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
                if self.transmitter_proc.poll() is not None:
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

                # Resolve stream path / url
                source_path = ""
                if track.get("is_tts"):
                    # Local synthesis audio path
                    source_path = track.get("path")
                    orchestrator.log(f"Injecting host break audio: {track.get('title')}", "system")
                else:
                    # YTMusic stream URL resolution
                    resolved_url = self.resolve_stream_url(track.get("track_id"))
                    if not resolved_url:
                        orchestrator.log(f"Could not resolve track: {track.get('title')}. Skipping.", "error")
                        continue
                    source_path = resolved_url
                    orchestrator.log(f"Broadcasting: '{track.get('title')}' by {track.get('artist')}", "system")

                # Spawn decoder subprocess to decode media stream to raw PCM s16le
                decode_cmd = [
                    "ffmpeg",
                    "-i", source_path,
                    "-f", "s16le",
                    "-ac", "2",
                    "-ar", "44100",
                    "pipe:1"
                ]

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
                chunk_size = 4096  # 1KB chunks for low latency skipping
                write_error = False

                while orchestrator.is_running:
                    # Check skip button command
                    if orchestrator.skip_requested:
                        orchestrator.log("Skip signal received. Terminating current track...", "system")
                        orchestrator.skip_requested = False
                        break

                    # Check if transmitter is alive
                    if self.transmitter_proc.poll() is not None:
                        orchestrator.log("Transmitter terminated during decoding. Aborting track.", "error")
                        write_error = True
                        break

                    # Read PCM chunk from decoder
                    chunk = self.decoder_proc.stdout.read(chunk_size)
                    if not chunk:
                        break  # Track finished decoding naturally

                    try:
                        self.transmitter_proc.stdin.write(chunk)
                        bytes_written += len(chunk)

                        # Calculate elapsed seconds: s16le (2ch, 2bytes, 44100Hz) = 176400 bytes/sec
                        elapsed = bytes_written / 176400
                        if int(elapsed) > int((bytes_written - len(chunk)) / 176400):
                            if orchestrator.on_progress:
                                orchestrator.on_progress(int(elapsed), track.get("duration_seconds", 0))

                    except OSError as e:
                        orchestrator.log(f"Pipe write error (broken stream): {e}", "error")
                        write_error = True
                        break

                # Clean up decoder
                try:
                    self.decoder_proc.terminate()
                    self.decoder_proc.wait(timeout=2)
                except Exception:
                    pass
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

        orchestrator.log("Master A/V transmitter loop stopped.", "system")

    def _log_ffmpeg_stderr(self, proc):
        """Reads FFmpeg transmitter logs line-by-line and feeds them to the console."""
        while True:
            line = proc.stderr.readline()
            if not line:
                break
            try:
                line_str = line.decode('utf-8', errors='ignore').strip()
                # Feed logs to dashboard console via websocket
                # Filter for useful info or let user see FFmpeg telemetry
                if "frame=" in line_str or "fps=" in line_str or "bitrate=" in line_str:
                    orchestrator.log(f"[FFmpeg] {line_str}", "ffmpeg")
                elif "Error" in line_str or "warning" in line_str:
                    orchestrator.log(f"[FFmpeg Warning] {line_str}", "error")
            except Exception:
                pass

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
                self.transmitter_proc.wait(timeout=3)
            except Exception:
                pass
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

# Create engine singleton
engine = StreamEngine()
