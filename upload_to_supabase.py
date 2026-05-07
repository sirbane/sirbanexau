"""
upload_to_supabase.py  ─  Sync local trading data to Supabase
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Reads scuro_live_data.json and uploads to Supabase every 10 seconds.
Keeps dashboard accessible from anywhere.

Run:
    python upload_to_supabase.py

(Runs in background alongside mt5_history.py)
"""

import json
import time
import os
from datetime import datetime
from supabase import create_client, Client

# ─── CONFIG ──────────────────────────────────────────────
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL", "https://amykrtiekuyccddwyvbb.supabase.co")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "sb_publishable_49Ob7v2rKNuzBaRaNGfhEg_DKoFqUQe")
DATA_FILE = os.getenv("DATA_FILE", "scuro_live_data.json")
TABLE_NAME = os.getenv("TABLE_NAME", "live_trading_data")
UPLOAD_INTERVAL = int(os.getenv("UPLOAD_INTERVAL", "10"))

# ─── INITIALIZE CLIENT ───────────────────────────────────
try:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    print("✓ Connected to Supabase")
except Exception as e:
    print(f"✗ Supabase connection failed: {e}")
    exit(1)


def ensure_table_exists():
    """Create table if it doesn't exist (one-time setup)."""
    try:
        # Try to query the table
        supabase.table(TABLE_NAME).select("*").limit(1).execute()
        print(f"✓ Table '{TABLE_NAME}' exists")
    except Exception as e:
        print(f"⚠ Table '{TABLE_NAME}' not found. Creating...")
        # Note: You'll need to create this manually in Supabase SQL editor:
        # CREATE TABLE live_trading_data (
        #   id INT PRIMARY KEY DEFAULT 1,
        #   data JSONB NOT NULL,
        #   updated_at TIMESTAMP DEFAULT NOW()
        # );
        # For now, we'll assume it exists or create via SQL in Supabase console


def upload_data():
    """Read local JSON and upload to Supabase."""
    if not os.path.exists(DATA_FILE):
        print(f"⚠ {DATA_FILE} not found. Waiting...")
        return False

    try:
        with open(DATA_FILE, "r") as f:
            data = json.load(f)

        # Upload to Supabase (upsert at id=1)
        supabase.table(TABLE_NAME).upsert({
            "id": 1,
            "data": data,
            "updated_at": datetime.utcnow().isoformat()
        }).execute()

        timestamp = datetime.now().strftime("%H:%M:%S")
        print(f"[{timestamp}] ✓ Uploaded to Supabase")
        return True

    except json.JSONDecodeError as e:
        print(f"✗ JSON parsing error: {e}")
        return False
    except Exception as e:
        print(f"✗ Upload failed: {e}")
        return False


def main():
    print(f"\n{'='*60}")
    print(f"SCURO SUPABASE UPLOADER")
    print(f"{'='*60}")
    print(f"Uploading {DATA_FILE} to Supabase every {UPLOAD_INTERVAL}s\n")

    ensure_table_exists()

    try:
        while True:
            upload_data()
            time.sleep(UPLOAD_INTERVAL)
    except KeyboardInterrupt:
        print("\n\n✓ Uploader stopped")


if __name__ == "__main__":
    main()
