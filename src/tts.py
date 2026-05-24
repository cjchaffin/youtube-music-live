import logging
import requests
from src.config import settings

logger = logging.getLogger("tts")

def generate_voiceover(text: str, filename: str = "host_break.wav") -> str:
    """
    Sends a synthesis request to the Piper TTS container.
    Saves the output WAV file to the tts directory and returns the absolute file path.
    If synthesis fails, logs the error and returns None.
    """
    output_path = settings.tts_dir_path / filename
    
    # Clean up previous file if it exists
    if output_path.exists():
        try:
            output_path.unlink()
        except Exception as e:
            logger.error(f"Failed to delete old voiceover asset: {e}")
            
    logger.info(f"Synthesizing voiceover via Piper TTS at {settings.PIPER_URL}...")
    logger.info(f"Script: '{text}'")
    
    try:
        # Standard Piper HTTP server endpoint is /?text=...
        response = requests.get(
            f"{settings.PIPER_URL}/", 
            params={"text": text}, 
            timeout=15
        )
        
        if response.status_code == 200 and response.content and len(response.content) > 0:
            with open(output_path, "wb") as f:
                f.write(response.content)
            logger.info(f"Voiceover successfully synthesized and saved to {output_path}")
            return str(output_path)
        else:
            logger.error(f"Piper TTS synthesis failed or returned empty content. Status code: {response.status_code}")
            return None
            
    except requests.exceptions.RequestException as e:
        logger.error(f"Could not connect to Piper TTS service at {settings.PIPER_URL}: {e}")
        return None
