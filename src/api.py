import asyncio
import json
import logging
import threading
from typing import Set
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Response
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from src.config import settings
from src.orchestrator import orchestrator
from src.stream import engine
from src.ingestion import fetch_liked_music, save_library

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("api")

app = FastAPI(title="YouTube Music Live Controller")

# WebSocket Connection Manager
class ConnectionManager:
    def __init__(self):
        self.active_connections: Set[WebSocket] = set()

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.add(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        if not self.active_connections:
            return
        
        # Convert dictionary to JSON string
        payload = json.dumps(message)
        
        # Gather all sends so they run concurrently
        disconnected = []
        for connection in self.active_connections:
            try:
                await connection.send_text(payload)
            except Exception:
                disconnected.append(connection)
                
        for connection in disconnected:
            self.disconnect(connection)

manager = ConnectionManager()

# Setup Orchestrator event hooks to forward telemetry to WebSockets
# Since thread callbacks run in background threads, we bridge them to FastAPI's event loop
event_loop = None

def run_async_coro(coro):
    """Helper to safely schedule a coroutine on the running FastAPI event loop."""
    global event_loop
    if event_loop is None:
        try:
            event_loop = asyncio.get_running_loop()
        except RuntimeError:
            pass
            
    if event_loop is None:
        coro.close()  # Close the coroutine to prevent "never awaited" warning
        return
    try:
        asyncio.run_coroutine_threadsafe(coro, event_loop)
    except Exception as e:
        logger.error(f"Failed to schedule WebSocket broadcast: {e}")

@app.on_event("startup")
async def startup_event():
    global event_loop
    event_loop = asyncio.get_running_loop()
    logger.info("FastAPI startup: main event loop captured successfully.")

# Register orchestrator event callbacks
orchestrator.on_track_change = lambda track: run_async_coro(
    manager.broadcast({"type": "track", "track": track})
)

orchestrator.on_progress = lambda elapsed, duration: run_async_coro(
    manager.broadcast({"type": "progress", "elapsed": elapsed, "duration": duration})
)

orchestrator.on_log = lambda message, level: run_async_coro(
    manager.broadcast({"type": "log", "message": message, "level": level})
)

# API Request Models
class ModeRequest(BaseModel):
    mode: str

class SeedRequest(BaseModel):
    query: str

class PlaylistRequest(BaseModel):
    url: str

class TtsToggleRequest(BaseModel):
    enabled: bool

class TtsFrequencyRequest(BaseModel):
    frequency: int

class VisualizerRequest(BaseModel):
    visualizer: str

# API Endpoints
@app.get("/api/status")
def get_status():
    return {
        "live": orchestrator.is_running,
        "current_track": orchestrator.current_track,
        "config": {
            "mode": orchestrator.mode,
            "tts_enabled": orchestrator.tts_enabled,
            "tts_frequency": orchestrator.tts_frequency,
            "seed_query": orchestrator.seed_query,
            "playlist_url": orchestrator.playlist_url,
            "visualizer": settings.STREAM_VISUALIZER
        }
    }

@app.post("/api/set-mode")
def set_mode(req: ModeRequest):
    if req.mode not in ["liked", "seed", "playlist"]:
        raise HTTPException(status_code=400, detail="Invalid mode. Select 'liked', 'seed', or 'playlist'.")
    orchestrator.set_mode(req.mode)
    return {"status": "success", "mode": req.mode}

@app.post("/api/set-visualizer")
def set_visualizer(req: VisualizerRequest):
    valid_visualizers = ["showfreqs", "showcqt", "showwaves", "avectorscope", "showspectrum", "none"]
    val = req.visualizer.lower()
    if val not in valid_visualizers:
        raise HTTPException(status_code=400, detail=f"Invalid visualizer. Options: {', '.join(valid_visualizers)}")
    engine.change_visualizer(val)
    run_async_coro(manager.broadcast({"type": "visualizer", "visualizer": val}))
    return {"status": "success", "visualizer": val}

@app.post("/api/set-seed")
def set_seed(req: SeedRequest):
    if not req.query.strip():
        raise HTTPException(status_code=400, detail="Seed query cannot be empty.")
    
    # Run fetch in a separate thread so it doesn't block the API
    def fetch_task():
        orchestrator.set_seed(req.query)
        
    threading_thread = threading.Thread(target=fetch_task)
    threading_thread.start()
    return {"status": "pending", "message": f"Fetching tracks for seed: {req.query}"}

@app.post("/api/set-playlist")
def set_playlist(req: PlaylistRequest):
    if not req.url.strip():
        raise HTTPException(status_code=400, detail="Playlist URL cannot be empty.")
        
    def fetch_task():
        orchestrator.set_playlist(req.url)
        
    threading_thread = threading.Thread(target=fetch_task)
    threading_thread.start()
    return {"status": "pending", "message": "Fetching playlist tracks"}

@app.post("/api/refresh-ingestion")
def refresh_ingestion():
    def fetch_task():
        orchestrator.log("Scraping Liked Music catalog...", "system")
        tracks = fetch_liked_music()
        if tracks:
            save_library(tracks)
            orchestrator.log(f"Synced {len(tracks)} liked music tracks.", "success")
        else:
            orchestrator.log("No liked music tracks found. Ensure config/auth.json is valid.", "error")
            
    threading_thread = threading.Thread(target=fetch_task)
    threading_thread.start()
    return {"status": "pending", "message": "Synchronizing library ingestion"}

@app.post("/api/toggle-stream")
def toggle_stream():
    if orchestrator.is_running:
        engine.stop()
    else:
        engine.start()
    
    # Broadcast stream status to all connected clients
    run_async_coro(manager.broadcast({"type": "status", "live": orchestrator.is_running}))
    return {"live": orchestrator.is_running}

@app.post("/api/skip-track")
def skip_track():
    if not orchestrator.is_running:
        raise HTTPException(status_code=400, detail="Stream is offline.")
    orchestrator.skip_requested = True
    return {"status": "skipped"}

@app.post("/api/toggle-tts")
def toggle_tts(req: TtsToggleRequest):
    orchestrator.tts_enabled = req.enabled
    orchestrator.log(f"Host voice synthesis { 'ENABLED' if req.enabled else 'DISABLED' }", "system")
    return {"tts_enabled": req.enabled}

@app.post("/api/set-tts-frequency")
def set_tts_frequency(req: TtsFrequencyRequest):
    if req.frequency < 2:
        raise HTTPException(status_code=400, detail="Frequency must be at least 2 tracks.")
    orchestrator.tts_frequency = req.frequency
    orchestrator.log(f"Set host break frequency to every {req.frequency} tracks.", "system")
    return {"tts_frequency": req.frequency}

@app.post("/api/trigger-tts-break")
def trigger_tts_break():
    if not orchestrator.is_running:
        raise HTTPException(status_code=400, detail="Stream is offline.")
    orchestrator.trigger_tts_break_now()
    return {"status": "scheduled"}

# WebSocket Telemetry Endpoint
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    global event_loop
    event_loop = asyncio.get_running_loop()
    await manager.connect(websocket)
    try:
        # Keep connection open and receive optional client messages
        while True:
            data = await websocket.receive_text()
            # Handle client-to-server WS messages if needed
            logger.info(f"Received WS payload: {data}")
    except WebSocketDisconnect:
        manager.disconnect(websocket)

@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(status_code=204)

# Serve index.html and web files
# Make sure we mount static folder after defining API routes to prevent catching them
web_dir = settings.PROJECT_ROOT / "web"
if web_dir.exists():
    app.mount("/", StaticFiles(directory=str(web_dir), html=True), name="web")
else:
    logger.error(f"Web assets directory not found at {web_dir}!")
