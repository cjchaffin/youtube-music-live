import sys
from pathlib import Path

# Add project root to python path if run directly
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(PROJECT_ROOT))

from ytmusicapi import YTMusic
from src.config import settings

def run_setup():
    print("==================================================================")
    print("            YouTube Music Live - Auth Bootstrap Helper            ")
    print("==================================================================")
    print(f"This tool will guide you in creating your request authentication file:")
    print(f"-> {settings.auth_json_path}")
    print("")
    print("Instructions:")
    print("1. Open Google Chrome or Firefox, go to https://music.youtube.com")
    print("2. Make sure you are logged in.")
    print("3. Press F12 (Developer Tools) and select the 'Network' tab.")
    print("4. Find a POST request (e.g. search for 'browse' or 'next' in filter).")
    print("5. Right-click the request, select Copy -> Copy request headers (or copy as curl/headers depending on browser).")
    print("6. Paste the headers below when prompted.")
    print("==================================================================\n")
    
    # Ensure the config folder exists
    settings.CONFIG_PATH.mkdir(parents=True, exist_ok=True)
    
    try:
        # In ytmusicapi, setup() waits for the user to paste headers via standard input
        # and parses them, writing the resulting JSON to the target filepath.
        # Note: Newer versions can also do setup via browser/oauth, but headers are most reliable for private library scraping.
        YTMusic.setup(filepath=str(settings.auth_json_path))
        print(f"\n[SUCCESS] Authentication headers successfully saved to: {settings.auth_json_path}")
        print("You can now copy this config directory to your NAS for deployment!")
    except KeyboardInterrupt:
        print("\n[INFO] Setup cancelled by user.")
    except Exception as e:
        print(f"\n[ERROR] Authentication setup failed: {e}")

if __name__ == "__main__":
    run_setup()
