import os
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

# Determine project base directory
PROJECT_ROOT = Path(__file__).resolve().parent.parent

class Settings(BaseSettings):
    # API Keys & Stream Configurations
    GEMINI_API_KEY: str = ""
    YOUTUBE_STREAM_KEY: str = ""
    PIPER_URL: str = "http://localhost:5000"
    
    # App Settings
    DASHBOARD_PORT: int = 8000
    DEBUG: bool = False
    
    # Path mappings (can be overridden, defaults to project root subdirectories)
    CONFIG_PATH: Path = PROJECT_ROOT / "config"
    DATA_PATH: Path = PROJECT_ROOT / "data"
    ASSETS_PATH: Path = PROJECT_ROOT / "assets"
    
    # Derived paths
    @property
    def auth_json_path(self) -> Path:
        return self.CONFIG_PATH / "auth.json"
        
    @property
    def library_json_path(self) -> Path:
        return self.DATA_PATH / "library.json"
        
    @property
    def playlist_txt_path(self) -> Path:
        return self.DATA_PATH / "playlist.txt"
        
    @property
    def tts_dir_path(self) -> Path:
        path = self.DATA_PATH / "tts"
        path.mkdir(parents=True, exist_ok=True)
        return path
        
    @property
    def canvas_static_path(self) -> Path:
        return self.ASSETS_PATH / "canvas_static.png"

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore"
    )

# Instantiate settings singleton
settings = Settings()
