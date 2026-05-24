// Global State
let currentMode = 'liked';
let isStreaming = false;
let ws = null;
let progressInterval = null;
let currentTrackDuration = 0;
let currentTrackElapsed = 0;

// Connect to WebSocket on load
window.addEventListener('DOMContentLoaded', () => {
    connectWebSocket();
    // Fetch initial status from API
    fetchInitialStatus();
});

function connectWebSocket() {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${window.location.host}/ws`;
    
    appendLog(`[SYSTEM] Connecting to WebSocket: ${wsUrl}`, 'system');
    
    ws = new WebSocket(wsUrl);
    
    ws.onopen = () => {
        appendLog('[SYSTEM] Connected to server telemetry feed.', 'success');
    };
    
    ws.onmessage = (event) => {
        const data = JSON.parse(event.data);
        handleTelemetryMessage(data);
    };
    
    ws.onclose = () => {
        appendLog('[SYSTEM] Connection lost. Attempting reconnect in 3s...', 'error');
        setTimeout(connectWebSocket, 3000);
    };
    
    ws.onerror = (err) => {
        console.error('WebSocket Error:', err);
    };
}

function handleTelemetryMessage(msg) {
    switch (msg.type) {
        case 'status':
            updateStreamStatus(msg.live);
            break;
            
        case 'track':
            updateNowPlaying(msg.track);
            break;
            
        case 'progress':
            updateProgress(msg.elapsed, msg.duration);
            break;
            
        case 'log':
            appendLog(msg.message, msg.level || 'ffmpeg');
            break;
            
        case 'config':
            updateConfigFields(msg.config);
            break;
            
        default:
            console.log('Unknown WebSocket message:', msg);
    }
}

// Fetch states when page loads
async function fetchInitialStatus() {
    try {
        const response = await fetch('/api/status');
        const data = await response.json();
        
        updateStreamStatus(data.live);
        if (data.current_track) {
            updateNowPlaying(data.current_track);
        }
        if (data.config) {
            updateConfigFields(data.config);
        }
    } catch (err) {
        appendLog(`[ERROR] Failed to fetch initial status: ${err.message}`, 'error');
    }
}

// UI updates
function updateStreamStatus(live) {
    isStreaming = live;
    const badge = document.getElementById('stream-status-badge');
    const badgeText = badge.querySelector('.badge-text');
    const toggleBtn = document.getElementById('btn-toggle-stream');
    
    if (live) {
        badge.className = 'badge live';
        badgeText.textContent = 'LIVE';
        toggleBtn.textContent = 'STOP LIVESTREAM';
        toggleBtn.className = 'btn-primary stop';
    } else {
        badge.className = 'badge offline';
        badgeText.textContent = 'OFFLINE';
        toggleBtn.textContent = 'START LIVESTREAM';
        toggleBtn.className = 'btn-primary start';
    }
}

function updateNowPlaying(track) {
    if (!track) {
        document.getElementById('track-title').textContent = 'SYSTEM INACTIVE';
        document.getElementById('track-artist').textContent = 'Start the stream to initiate playlist';
        document.getElementById('track-album').textContent = '-';
        document.getElementById('track-art').src = 'https://images.unsplash.com/photo-1614613535308-eb5fbd3d2c17?q=80&w=300&auto=format&fit=crop';
        updateProgress(0, 0);
        return;
    }
    
    document.getElementById('track-title').textContent = track.title || 'Unknown Track';
    document.getElementById('track-artist').textContent = track.artist || 'Unknown Artist';
    document.getElementById('track-album').textContent = track.album || 'Unknown Album';
    if (track.thumbnail_url) {
        document.getElementById('track-art').src = track.thumbnail_url;
    } else {
        document.getElementById('track-art').src = 'https://images.unsplash.com/photo-1614613535308-eb5fbd3d2c17?q=80&w=300&auto=format&fit=crop';
    }
}

function updateProgress(elapsed, duration) {
    currentTrackElapsed = elapsed;
    currentTrackDuration = duration;
    
    const progressBar = document.getElementById('track-progress');
    const currentText = document.getElementById('time-current');
    const totalText = document.getElementById('time-total');
    
    if (duration <= 0) {
        progressBar.style.width = '0%';
        currentText.textContent = '0:00';
        totalText.textContent = '0:00';
        clearInterval(progressInterval);
        return;
    }
    
    const percent = Math.min((elapsed / duration) * 100, 100);
    progressBar.style.width = `${percent}%`;
    currentText.textContent = formatTime(elapsed);
    totalText.textContent = formatTime(duration);
    
    // Smooth progress simulation between intervals
    clearInterval(progressInterval);
    if (isStreaming) {
        const start = Date.now();
        progressInterval = setInterval(() => {
            const extraElapsed = Math.floor((Date.now() - start) / 1000);
            const simulatedElapsed = Math.min(elapsed + extraElapsed, duration);
            const simulatedPercent = (simulatedElapsed / duration) * 100;
            progressBar.style.width = `${simulatedPercent}%`;
            currentText.textContent = formatTime(simulatedElapsed);
            
            if (simulatedElapsed >= duration) {
                clearInterval(progressInterval);
            }
        }, 1000);
    }
}

function updateConfigFields(config) {
    if (config.mode) {
        setModeUI(config.mode);
    }
    if (config.tts_enabled !== undefined) {
        document.getElementById('tts-enabled').checked = config.tts_enabled;
    }
    if (config.tts_frequency) {
        document.getElementById('tts-frequency').value = config.tts_frequency;
    }
    if (config.seed_query) {
        document.getElementById('seed-query').value = config.seed_query;
    }
    if (config.playlist_url) {
        document.getElementById('playlist-url').value = config.playlist_url;
    }
}

function setModeUI(mode) {
    currentMode = mode;
    
    // Toggle active classes on buttons
    document.getElementById('mode-liked').classList.remove('active');
    document.getElementById('mode-seed').classList.remove('active');
    document.getElementById('mode-playlist').classList.remove('active');
    document.getElementById(`mode-${mode}`).classList.add('active');
    
    // Toggle visibility of input groups
    document.getElementById('input-group-liked').classList.add('hidden');
    document.getElementById('input-group-seed').classList.add('hidden');
    document.getElementById('input-group-playlist').classList.add('hidden');
    document.getElementById(`input-group-${mode}`).classList.remove('hidden');
}

// User Actions
async function setMode(mode) {
    setModeUI(mode);
    try {
        await fetch('/api/set-mode', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ mode })
        });
        appendLog(`[SYSTEM] Swapped source mode to: ${mode.toUpperCase()}`, 'system');
    } catch (err) {
        appendLog(`[ERROR] Failed to set mode: ${err.message}`, 'error');
    }
}

async function applySeed() {
    const query = document.getElementById('seed-query').value.trim();
    if (!query) return;
    
    try {
        appendLog(`[SYSTEM] Applying seed prompt: "${query}"...`, 'system');
        const response = await fetch('/api/set-seed', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ query })
        });
        const data = await response.json();
        appendLog(`[SYSTEM] Loaded ${data.count} tracks from seed.`, 'success');
    } catch (err) {
        appendLog(`[ERROR] Failed to apply seed: ${err.message}`, 'error');
    }
}

async function applyPlaylist() {
    const url = document.getElementById('playlist-url').value.trim();
    if (!url) return;
    
    try {
        appendLog(`[SYSTEM] Loading custom playlist: ${url}...`, 'system');
        const response = await fetch('/api/set-playlist', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ url })
        });
        const data = await response.json();
        appendLog(`[SYSTEM] Loaded ${data.count} tracks from playlist.`, 'success');
    } catch (err) {
        appendLog(`[ERROR] Failed to load playlist: ${err.message}`, 'error');
    }
}

async function refreshIngestion() {
    try {
        appendLog('[SYSTEM] Refreshing liked music catalog from YTMusic...', 'system');
        const response = await fetch('/api/refresh-ingestion', { method: 'POST' });
        const data = await response.json();
        appendLog(`[SYSTEM] Refreshed library. Saved ${data.count} verified tracks.`, 'success');
    } catch (err) {
        appendLog(`[ERROR] Ingestion sync failed: ${err.message}`, 'error');
    }
}

async function toggleStream() {
    try {
        const action = isStreaming ? 'stop' : 'start';
        appendLog(`[SYSTEM] Triggering stream ${action.toUpperCase()} command...`, 'system');
        const response = await fetch('/api/toggle-stream', { method: 'POST' });
        const data = await response.json();
        updateStreamStatus(data.live);
    } catch (err) {
        appendLog(`[ERROR] Failed to toggle stream: ${err.message}`, 'error');
    }
}

async function skipTrack() {
    if (!isStreaming) {
        appendLog('[SYSTEM] Stream is offline. Cannot skip track.', 'system');
        return;
    }
    try {
        appendLog('[SYSTEM] Sending skip track command...', 'system');
        await fetch('/api/skip-track', { method: 'POST' });
    } catch (err) {
        appendLog(`[ERROR] Failed to skip track: ${err.message}`, 'error');
    }
}

async function toggleTTS() {
    const enabled = document.getElementById('tts-enabled').checked;
    try {
        await fetch('/api/toggle-tts', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled })
        });
        appendLog(`[SYSTEM] Voiceovers ${enabled ? 'ENABLED' : 'DISABLED'}`, 'system');
    } catch (err) {
        appendLog(`[ERROR] Failed to toggle voiceover: ${err.message}`, 'error');
    }
}

async function updateTTSFrequency() {
    const frequency = parseInt(document.getElementById('tts-frequency').value);
    if (isNaN(frequency) || frequency < 2) return;
    
    try {
        await fetch('/api/set-tts-frequency', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ frequency })
        });
        appendLog(`[SYSTEM] Set host break frequency to every ${frequency} tracks.`, 'system');
    } catch (err) {
        appendLog(`[ERROR] Failed to set break frequency: ${err.message}`, 'error');
    }
}

async function triggerManualBreak() {
    if (!isStreaming) {
        appendLog('[SYSTEM] Stream is offline. Cannot trigger voice break.', 'system');
        return;
    }
    try {
        appendLog('[SYSTEM] Injecting manual voice break on next track interval...', 'system');
        await fetch('/api/trigger-tts-break', { method: 'POST' });
    } catch (err) {
        appendLog(`[ERROR] Failed to trigger voice break: ${err.message}`, 'error');
    }
}

// Helpers
function appendLog(message, level) {
    const consoleBody = document.getElementById('console-logs');
    const logDiv = document.createElement('div');
    logDiv.className = `log-line ${level}`;
    logDiv.textContent = message;
    consoleBody.appendChild(logDiv);
    
    // Auto-scroll to bottom
    consoleBody.scrollTop = consoleBody.scrollHeight;
    
    // Limit console buffer size to 200 lines
    while (consoleBody.children.length > 200) {
        consoleBody.removeChild(consoleBody.firstChild);
    }
}

function formatTime(secs) {
    if (isNaN(secs) || secs < 0) return '0:00';
    const m = Math.floor(secs / 60);
    const s = Math.floor(secs % 60);
    return `${m}:${s < 10 ? '0' : ''}${s}`;
}
