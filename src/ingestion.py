import os
import re
import json
import logging
from typing import List, Dict, Any, Optional
from ytmusicapi import YTMusic
from src.config import settings

# Set up logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("ingestion")

def get_ytmusic_client() -> YTMusic:
    """
    Initializes and returns the YTMusic client.
    Uses authenticated session headers if auth.json exists, otherwise initializes unauthenticated.
    """
    auth_path = settings.auth_json_path
    if auth_path.exists():
        logger.info(f"Initializing YTMusic with authentication from {auth_path}")
        try:
            return YTMusic(str(auth_path))
        except Exception as e:
            logger.error(f"Failed to load authenticated YTMusic client: {e}. Falling back to unauthenticated.")
    else:
        logger.warning(f"No auth.json found at {auth_path}. Running in unauthenticated mode.")
    
    return YTMusic()

def clean_playlist_id(playlist_url_or_id: str) -> str:
    """
    Extracts the playlist ID from a YouTube Music/YouTube URL or returns it directly if already an ID.
    """
    if "list=" in playlist_url_or_id:
        match = re.search(r"list=([^&]+)", playlist_url_or_id)
        if match:
            return match.group(1)
    return playlist_url_or_id

def verify_track_safety(track: Dict[str, Any]) -> bool:
    """
    Safety Rulecheck Protocol:
    1. Checks if track contains videoId and artists.
    2. Validates that the artist has a valid browseId (starting with UC or FUrartist_).
    3. Flags and removes community video uploads by checking if they lack canonical albums or official artist browse IDs.
    """
    try:
        video_id = track.get("videoId")
        if not video_id:
            return False
            
        # Check artists
        artists = track.get("artists") or []
        if not isinstance(artists, list) or not artists:
            logger.debug(f"Track {video_id} ('{track.get('title')}') has no artists or invalid format. Flagged unsafe.")
            return False
            
        # Ensure at least one artist has a valid browseId (official artist or topic)
        has_official_artist = False
        for artist in artists:
            if isinstance(artist, dict):
                artist_id = artist.get("id")
                if artist_id and (artist_id.startswith("UC") or artist_id.startswith("FUrartist_")):
                    has_official_artist = True
                    break
            elif isinstance(artist, str):
                pass
                
        if not has_official_artist:
            logger.debug(f"Track {video_id} ('{track.get('title')}') lacks official artist browseId. Flagged unsafe.")
            return False
            
        # Ensure it has a canonical release record (an album is present for official songs)
        album = track.get("album")
        if not album:
            logger.debug(f"Track {video_id} ('{track.get('title')}') lacks album metadata. Flagged unsafe.")
            return False
            
        album_name = None
        if isinstance(album, dict):
            album_name = album.get("name")
        elif isinstance(album, str):
            album_name = album
            
        if not album_name:
            logger.debug(f"Track {video_id} ('{track.get('title')}') lacks album name. Flagged unsafe.")
            return False
            
        return True
    except Exception as e:
        logger.error(f"Error validating track safety: {e}")
        return False

def parse_track_data(track: Dict[str, Any]) -> Dict[str, Any]:
    """
    Parses a raw YTMusic track object into our validated schema.
    """
    artists = track.get("artists") or []
    artist_names = []
    if isinstance(artists, list):
        for a in artists:
            if isinstance(a, dict):
                artist_names.append(a.get("name") or "Unknown Artist")
            elif isinstance(a, str):
                artist_names.append(a)
    else:
        artist_names.append("Unknown Artist")
        
    if not artist_names:
        artist_names.append("Unknown Artist")
    artist_str = ", ".join(artist_names)
    
    # Get thumbnail URL
    thumbnails = track.get("thumbnails") or []
    thumbnail_url = ""
    if isinstance(thumbnails, list) and thumbnails:
        last_thumb = thumbnails[-1]
        if isinstance(last_thumb, dict):
            thumbnail_url = last_thumb.get("url") or ""
    
    # Handle album
    album = track.get("album")
    album_name = "Unknown Album"
    if isinstance(album, dict):
        album_name = album.get("name") or "Unknown Album"
    elif isinstance(album, str):
        album_name = album
        
    return {
        "track_id": track.get("videoId"),
        "title": track.get("title", "Unknown Title"),
        "artist": artist_str,
        "album": album_name,
        "duration_seconds": track.get("duration_seconds", 0),
        "thumbnail_url": thumbnail_url,
        "safety_status": "verified"
    }

def load_library_fallback() -> List[Dict[str, Any]]:
    """
    Loads cached tracks from library.json as a fallback.
    """
    library_path = settings.library_json_path
    if library_path.exists():
        try:
            with open(library_path, "r", encoding="utf-8") as f:
                tracks = json.load(f)
            logger.info(f"Loaded {len(tracks)} fallback tracks from local library cache.")
            return tracks
        except Exception as e:
            logger.error(f"Failed to load fallback library: {e}")
    return []

def save_library(tracks: List[Dict[str, Any]]) -> None:
    """
    Saves the list of verified tracks to data/library.json.
    """
    settings.DATA_PATH.mkdir(parents=True, exist_ok=True)
    library_path = settings.library_json_path
    
    try:
        with open(library_path, "w", encoding="utf-8") as f:
            json.dump(tracks, f, indent=2, ensure_ascii=False)
        logger.info(f"Successfully saved {len(tracks)} verified tracks to {library_path}")
    except Exception as e:
        logger.error(f"Failed to save library.json: {e}")

def fetch_liked_music() -> List[Dict[str, Any]]:
    """
    Fetches the user's liked music catalog and filters for safe tracks.
    """
    yt = get_ytmusic_client()
    if not settings.auth_json_path.exists():
        logger.error("Cannot fetch Liked Music in unauthenticated mode.")
        return []
        
    logger.info("Fetching liked music catalog...")
    try:
        # Get liked songs (fetches up to 1000 tracks)
        raw_tracks = yt.get_liked_songs(limit=1000).get("tracks", [])
        logger.info(f"Fetched {len(raw_tracks)} raw tracks from Liked Music.")
        
        verified_tracks = []
        for t in raw_tracks:
            if verify_track_safety(t):
                verified_tracks.append(parse_track_data(t))
            else:
                logger.debug(f"Filtered out unsafe/unofficial track: {t.get('title')}")
                
        logger.info(f"Filtered liked music: {len(verified_tracks)} verified tracks out of {len(raw_tracks)}.")
        return verified_tracks
    except Exception as e:
        logger.error(f"Error fetching liked music: {e}")
        return []

def fetch_playlist(playlist_url_or_id: str) -> List[Dict[str, Any]]:
    """
    Fetches tracks from a specific playlist URL or ID and filters for safe tracks.
    """
    yt = get_ytmusic_client()
    playlist_id = clean_playlist_id(playlist_url_or_id)
    logger.info(f"Fetching playlist {playlist_id}...")
    
    try:
        # Fetch playlist details
        playlist_data = yt.get_playlist(playlist_id, limit=1000)
        raw_tracks = playlist_data.get("tracks", [])
        logger.info(f"Fetched {len(raw_tracks)} raw tracks from playlist '{playlist_data.get('title', 'Unknown')}'")
        
        verified_tracks = []
        for t in raw_tracks:
            if verify_track_safety(t):
                verified_tracks.append(parse_track_data(t))
            else:
                logger.debug(f"Filtered out unsafe/unofficial track: {t.get('title')}")
                
        logger.info(f"Filtered playlist: {len(verified_tracks)} verified tracks out of {len(raw_tracks)}.")
        return verified_tracks
    except Exception as e:
        logger.error(f"Error fetching playlist {playlist_id}: {e}")
        return []

def fetch_artist_genre_radio(query: str) -> List[Dict[str, Any]]:
    """
    Searches for a genre or artist query and gets a list of relevant tracks
    by playing a "watch playlist" (radio queue) starting from the top match.
    """
    yt = get_ytmusic_client()
    logger.info(f"Generating radio queue for seed query: '{query}'...")
    
    try:
        # 1. Search for songs matching the query
        search_results = yt.search(query, filter="songs")
        if not search_results:
            logger.warning(f"No songs found matching query '{query}'. Trying search without filters.")
            search_results = yt.search(query)
            
        # Find the first item with a videoId
        seed_video_id = None
        for result in search_results:
            if result.get("videoId"):
                seed_video_id = result["videoId"]
                logger.info(f"Found seed song: '{result.get('title')}' by {result.get('artists', [{}])[0].get('name')} (ID: {seed_video_id})")
                break
                
        if not seed_video_id:
            logger.error(f"No seed track found for query '{query}'")
            return []
            
        # 2. Get the watch playlist (radio queue) starting from this song
        # get_watch_playlist behaves like the "radio" feed for a specific track.
        raw_tracks = []
        try:
            radio_data = yt.get_watch_playlist(videoId=seed_video_id, limit=50)
            raw_tracks = radio_data.get("tracks", [])
            logger.info(f"Fetched {len(raw_tracks)} tracks from watch playlist (radio).")
        except Exception as re:
            logger.warning(f"Failed to fetch watch playlist (radio): {re}. Falling back to search results.")
        
        verified_tracks = []
        for t in raw_tracks:
            # get_watch_playlist returns raw track data that might be slightly different,
            # ensure videoId is populated (it might return videoId as videoId)
            if verify_track_safety(t):
                verified_tracks.append(parse_track_data(t))
            else:
                logger.debug(f"Filtered out unsafe/unofficial track: {t.get('title')}")
                
        # If we got no verified tracks from the radio feed, fall back to adding search results themselves
        if not verified_tracks:
            logger.warning("Radio queue filtering returned 0 verified tracks. Falling back to search results.")
            for t in search_results:
                if t.get("videoId") and verify_track_safety(t):
                    verified_tracks.append(parse_track_data(t))
                    
        logger.info(f"Filtered radio queue: {len(verified_tracks)} verified tracks.")
        return verified_tracks
    except Exception as e:
        logger.error(f"Error generating radio queue for '{query}': {e}")
        return []

if __name__ == "__main__":
    # Test execution
    print("Testing Ingestion Service...")
    client = get_ytmusic_client()
    print("Client initialized.")
