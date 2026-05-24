import os
import re
import random
import logging
import threading
import time
from typing import List, Dict, Any, Optional
from google import genai
from google.genai import types

from src.config import settings
from src.ingestion import (
    fetch_liked_music,
    fetch_artist_genre_radio,
    fetch_playlist,
    get_ytmusic_client,
    load_library_fallback
)
from src.tts import generate_voiceover

logger = logging.getLogger("orchestrator")

# Fallback radio host break announcements
FALLBACK_SCRIPTS = [
    "Terminal Flow Radio. Keep your focus absolute. The grid is aligned.",
    "Telemetry status nominal. Optimal trajectories calculated. Maintain coding velocity.",
    "Scanning catalog frequencies. Running background optimization. Stay in the zone.",
    "System telemetry checks out. Stream optimization complete. Continue the flow.",
    "Physical boundaries reached. Accessing deep subroutines. Focus is our only directive."
]

class StreamOrchestrator:
    def __init__(self):
        self.mode = "liked"
        self.seed_query = ""
        self.playlist_url = ""
        
        self.tts_enabled = True
        self.tts_frequency = 4
        self.tracks_since_last_tts = 0
        self.force_tts_break = False
        
        self.current_track: Optional[Dict[str, Any]] = None
        self.queue: List[Dict[str, Any]] = []
        self.history: List[Dict[str, Any]] = []
        
        self.is_running = False
        self.skip_requested = False
        self.active_decoder = None
        
        self.is_replenishing = False
        self.last_replenish_attempt = 0.0
        self.replenish_failure_count = 0
        
        self._lock = threading.Lock()
        
        # Telemetry updates callback (will hook into FastAPI WebSockets)
        self.on_track_change = None
        self.on_progress = None
        self.on_log = None
        
    def log(self, message: str, level: str = "system"):
        """Logs a message and broadcasts it via callback if set."""
        logger.info(message)
        if self.on_log:
            self.on_log(message, level)

    def set_mode(self, mode: str):
        with self._lock:
            self.mode = mode
            self.queue.clear()
        self.log(f"Stream mode updated to: {mode.upper()}", "system")
        
    def set_seed(self, query: str) -> int:
        with self._lock:
            self.seed_query = query
            self.mode = "seed"
            self.queue.clear()
        self.log(f"Generating seed playlist for: '{query}'", "system")
        tracks = fetch_artist_genre_radio(query)
        if tracks:
            with self._lock:
                self.queue = list(tracks)
            self.log(f"Loaded {len(tracks)} seed tracks into queue.", "success")
        return len(tracks)
        
    def set_playlist(self, url: str) -> int:
        with self._lock:
            self.playlist_url = url
            self.mode = "playlist"
            self.queue.clear()
        self.log(f"Loading playlist URL: {url}", "system")
        tracks = fetch_playlist(url)
        if tracks:
            with self._lock:
                self.queue = list(tracks)
            self.log(f"Loaded {len(tracks)} playlist tracks into queue.", "success")
        return len(tracks)

    def get_next_track(self) -> Optional[Dict[str, Any]]:
        """
        Retrieves the next track from the queue, or populates it
        if empty based on the current mode.
        """
        # Determine if we need to replenish without holding the lock for network calls
        must_replenish = False
        now = time.time()
        with self._lock:
            if not self.queue and not self.is_replenishing:
                # Exponential backoff cooldown: 3s, 6s, 12s, 24s, up to 60s max
                cooldown = min(60, 3 * (2 ** self.replenish_failure_count)) if self.replenish_failure_count > 0 else 0
                if now - self.last_replenish_attempt >= cooldown:
                    must_replenish = True
                    self.is_replenishing = True
                    self.last_replenish_attempt = now

        if must_replenish:
            try:
                tracks = self._fetch_tracks_for_replenish()
                with self._lock:
                    if tracks:
                        self.queue = list(tracks)
                        self.replenish_failure_count = 0
                    else:
                        self.replenish_failure_count += 1
            finally:
                with self._lock:
                    self.is_replenishing = False

        with self._lock:
            if self.queue:
                # Pop next track
                track = self.queue.pop(0)
                self.history.append(track)
                if len(self.history) > 100:
                    self.history.pop(0)
                return track
        return None

    def _fetch_tracks_for_replenish(self) -> List[Dict[str, Any]]:
        """Fetches tracks depending on active mode (does NOT require lock, no lock held)"""
        with self._lock:
            mode = self.mode
            seed_query = self.seed_query
            playlist_url = self.playlist_url

        self.log(f"Music queue exhausted. Replenishing catalog for mode '{mode.upper()}'...", "system")
        try:
            tracks = []
            if mode == "liked":
                tracks = fetch_liked_music()
                if tracks:
                    # Shuffle to keep liked catalog fresh
                    random.shuffle(tracks)
            elif mode == "seed" and seed_query:
                # Fetch radio recommendations based on query
                tracks = fetch_artist_genre_radio(seed_query)
            elif mode == "playlist" and playlist_url:
                tracks = fetch_playlist(playlist_url)

            if tracks:
                return tracks

            # If fetch failed or returned no tracks, attempt local library fallback
            self.log("Replenishment yielded no tracks from API. Attempting local library fallback...", "warning")
            fallback_tracks = load_library_fallback()
            if fallback_tracks:
                self.log(f"Successfully loaded {len(fallback_tracks)} fallback tracks from cache.", "success")
                return fallback_tracks
        except Exception as e:
            self.log(f"Error replenishing music queue: {e}", "error")
        return []

    def generate_host_script(self) -> str:
        """
        Calls Gemini to construct a low-frequency, calm, cyberpunk script.
        """
        if not settings.GEMINI_API_KEY:
            self.log("No GEMINI_API_KEY set. Using local fallback script.", "system")
            return random.choice(FALLBACK_SCRIPTS)
            
        self.log("Requesting host break script from Gemini 1.5 Flash...", "system")
        
        try:
            client = genai.Client(api_key=settings.GEMINI_API_KEY)
            
            system_instruction = (
                "You are the automated radio announcer for 'Terminal Flow Radio'. "
                "Your audience consists of software developers, automation engineers, drone pilots, and technical hobbyists. "
                "Your tone matrix is: low-frequency, extremely calm, deeply technical, and cyberpunk-adjacent. "
                "Always speak in the third person. Keep announcements very brief (maximum 15 words). "
                "Focus themes exclusively on: flow states, optimal automation trajectories, system telemetry, "
                "or physical limits. Never use radio clichés, hyperbole, or enthusiastic greetings."
            )
            
            prompt = (
                "Generate one short host break announcement. Do not include quotes or intros. "
                "Keep it under 15 words. Example: 'Telemetry status nominal. Optimal trajectories calculated. Maintain coding velocity.'"
            )
            
            response = client.models.generate_content(
                model="gemini-1.5-flash",
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    temperature=0.7,
                    max_output_tokens=30
                )
            )
            
            script = response.text.strip() if response.text else ""
            if script:
                # Clean enclosing quotes if Gemini adds them
                script = re.sub(r'^["\']|["\']$', '', script)
                self.log(f"Gemini script: '{script}'", "system")
                return script
        except Exception as e:
            self.log(f"Gemini script generation failed: {e}. Using fallback.", "error")
            
        return random.choice(FALLBACK_SCRIPTS)

    def trigger_tts_break_now(self):
        with self._lock:
            self.force_tts_break = True
        self.log("Manual host break requested for the next track interval.", "system")

    def get_voiceover_track(self) -> Optional[Dict[str, Any]]:
        """
        Checks if a host voice break is scheduled.
        If yes, generates the script and TTS WAV, then returns a mock track object.
        """
        should_break = False
        with self._lock:
            self.tracks_since_last_tts += 1
            if self.tts_enabled and (self.tracks_since_last_tts >= self.tts_frequency or self.force_tts_break):
                should_break = True
                self.tracks_since_last_tts = 0
                self.force_tts_break = False
                
        if not should_break:
            return None
            
        self.log("Host break scheduled. Generating voiceover...", "system")
        script = self.generate_host_script()
        wav_path = generate_voiceover(script)
        
        if wav_path and os.path.exists(wav_path):
            # Return a mock track representing the voice break
            return {
                "track_id": "tts_break",
                "title": f"[Host Break] {script}",
                "artist": "Terminal Flow Host",
                "album": "Piper TTS Engine",
                "duration_seconds": 10,  # Estimated fallback, but progress will count real bytes
                "path": wav_path,
                "is_tts": True
            }
        else:
            self.log("Voiceover synthesis failed. Skipping host break.", "error")
            return None

# Create orchestrator singleton
orchestrator = StreamOrchestrator()
