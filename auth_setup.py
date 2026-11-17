"""
OAuth2 Setup Script — Run this LOCALLY to generate token JSON
for each YouTube channel.

Usage:
    python auth_setup.py --channel 1
    python auth_setup.py --channel 2

Prerequisites:
    1. Go to https://console.cloud.google.com
    2. Create a new project (or use an existing one)
    3. Enable "YouTube Data API v3"
    4. Go to Credentials → Create Credentials → OAuth 2.0 Client ID
    5. Choose "Desktop App" as the application type
    6. Download the client_secret JSON file

This script will:
    - Open your browser for Google authentication
    - Generate a token JSON string
    - Print it so you can paste it into Render's env vars
"""

import argparse
import json
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def authenticate(client_secret_file: str, channel: int, force: bool = False):
    """Run the OAuth2 flow and print/save the resulting token JSON."""

    print(f"\n{'='*65}")
    print(f"  YouTube OAuth2 Setup — Channel {channel}")
    print(f"{'='*65}\n")

    token_file = Path(f"token_channel_{channel}.json")

    creds = None

    # Check for existing token unless force is requested
    if token_file.exists() and not force:
        print(f"📄 Found existing token file: {token_file}")
        try:
            creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
            if creds and creds.expired and creds.refresh_token:
                print("🔄 Refreshing expired token...")
                creds.refresh(Request())
                print("✅ Token refreshed successfully!")
        except Exception as e:
            print(f"⚠️ Existing token invalid ({e}). Starting fresh authentication...")
            creds = None

    if not creds or not creds.valid:
        print("🌐 Starting OAuth flow for Channel {channel}...")
        print("   A browser window will open automatically.")
        print("   If it does not, copy and open the link below in your browser.\n")

        flow = InstalledAppFlow.from_client_secrets_file(client_secret_file, SCOPES)
        creds = flow.run_local_server(
            port=0,
            prompt="consent",
            access_type="offline",
        )

    # Save token locally
    with open(token_file, "w", encoding="utf-8") as f:
        f.write(creds.to_json())

    # Auto-update .env file
    token_json_str = json.dumps(json.loads(creds.to_json()))
    client_secret_str = ""
    try:
        with open(client_secret_file, "r", encoding="utf-8") as f:
            client_secret_str = json.dumps(json.load(f))
    except Exception:
        pass

    env_path = Path(".env")
    if env_path.exists():
        try:
            import re
            env_content = env_path.read_text(encoding="utf-8")
            t_key = f"YOUTUBE_TOKEN_{channel}"
            s_key = f"YOUTUBE_CLIENT_SECRET_{channel}"
            if f"{t_key}=" in env_content:
                env_content = re.sub(rf"{t_key}=.*", f"{t_key}={token_json_str}", env_content)
            else:
                env_content += f"\n{t_key}={token_json_str}\n"

            if client_secret_str and f"{s_key}=" in env_content:
                env_content = re.sub(rf"{s_key}=.*", f"{s_key}={client_secret_str}", env_content)
            elif client_secret_str:
                env_content += f"\n{s_key}={client_secret_str}\n"

            env_path.write_text(env_content, encoding="utf-8")
            print(f"📝 Auto-updated {t_key} and {s_key} in .env file!")
        except Exception as e:
            print(f"⚠️ Could not auto-write to .env: {e}")

    print(f"\n✅ SUCCESS: Channel {channel} authenticated!")
    print(f"📁 Token saved locally to: {token_file}")
    print(f"🎉 The automation pipeline will automatically detect and use Channel {channel}.\n")



def main():
    parser = argparse.ArgumentParser(
        description="Generate YouTube OAuth2 tokens for the automation bot"
    )
    parser.add_argument(
        "--channel",
        type=int,
        required=True,
        help="Which channel to authenticate (e.g., 1, 2, 3)",
    )
    parser.add_argument(
        "--client-secret",
        type=str,
        default="client_secret.json",
        help="Path to the OAuth2 client secret JSON file (default: client_secret.json)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force a new OAuth login even if token already exists",
    )

    args = parser.parse_args()
    authenticate(args.client_secret, args.channel, force=args.force)



if __name__ == "__main__":
    main()
