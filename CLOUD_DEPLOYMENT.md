"""
SCURO CLOUD DEPLOYMENT GUIDE
═══════════════════════════════════════════════════════════════════

How to make your dashboard accessible from anywhere.

Architecture:
  MT5 Terminal (Windows PC)
    ↓
  mt5_history.py (generates scuro_live_data.json)
    ↓
  upload_to_supabase.py (uploads JSON to cloud every 10s)
    ↓
  Supabase Database (live_trading_data table)
    ↓
  dashboard_cloud.py (Streamlit Cloud)
    ↓
  Your Dashboard URL (accessible worldwide 24/7)

═══════════════════════════════════════════════════════════════════
"""

# ─── STEP 1: SET UP SUPABASE TABLE ───────────────────────────────

"""
Go to https://app.supabase.com

1. Open SQL Editor
2. Paste this SQL and execute:

  CREATE TABLE IF NOT EXISTS live_trading_data (
    id INT PRIMARY KEY DEFAULT 1,
    data JSONB NOT NULL,
    updated_at TIMESTAMP DEFAULT NOW()
  );

3. Verify the table exists in the Tables view

Your credentials are:
  - URL: https://amykrtiekuyccddwyvbb.supabase.co
  - Key: sb_publishable_49Ob7v2rKNuzBaRaNGfhEg_DKoFqUQe
  - Table: live_trading_data
"""

# ─── STEP 2: INSTALL DEPENDENCIES LOCALLY ────────────────────────

"""
Run this in your sir_bane_v1 folder:

  pip install supabase
  pip install -r requirements.txt
"""

# ─── STEP 3: RUN UPLOADER ON YOUR PC ─────────────────────────────

"""
Open PowerShell in your sir_bane_v1 folder and run:

  python upload_to_supabase.py

You should see:
  ✓ Connected to Supabase
  ✓ Table 'live_trading_data' exists
  [HH:MM:SS] ✓ Uploaded to Supabase
  [HH:MM:SS] ✓ Uploaded to Supabase
  ...

Keep this running alongside mt5_history.py:
  - Terminal 1: python mt5_history.py
  - Terminal 2: python upload_to_supabase.py

The uploader will continuously sync your latest data to the cloud.
"""

# ─── STEP 4: DEPLOY DASHBOARD TO STREAMLIT CLOUD ──────────────────

"""
A. Create Streamlit Cloud account
   - Go to https://streamlit.io/cloud
   - Sign up with GitHub

B. Push this folder to GitHub
   (or create a new repo with these files)

C. Deploy on Streamlit Cloud
   - Go to https://share.streamlit.io/
   - Click "New app"
   - Repository: your-username/sir_bane_v1
   - Branch: main
   - Main file path: dashboard_cloud.py
   - Click "Deploy"

D. Set environment variables
   - Go to your app settings (⋮ menu)
   - Secrets (Advanced)
   - Add these variables:

     SUPABASE_URL = "https://amykrtiekuyccddwyvbb.supabase.co"
     SUPABASE_KEY = "sb_publishable_49Ob7v2rKNuzBaRaNGfhEg_DKoFqUQe"

   - Click "Save"

E. Your dashboard is now live!
   - URL will look like: https://share.streamlit.io/your-username/sir_bane_v1/main/dashboard_cloud.py
   - Share this URL with anyone
"""

# ─── STEP 5: KEEP IT RUNNING 24/7 ──────────────────────────────────

"""
For the uploader to run continuously:

Option A: Windows Task Scheduler (recommended for always-on PC)
  1. Create batch file (upload_bot.bat):
     
     @echo off
     cd "C:\Users\obung\OneDrive\Documents\Trading Apps\sir_bane_v1"
     python upload_to_supabase.py
     pause

  2. Open Task Scheduler
  3. Create Basic Task → "Upload to Supabase"
  4. Trigger: At startup (or specific time)
  5. Action: Start a program → path to upload_bot.bat
  6. ✓ Run with highest privileges

Option B: PowerShell Persistent Terminal
  - Keep PowerShell open with the uploader running
  - Can minimize to taskbar

Option C: Cloud VM (if you want PC to be off)
  - Host upload_to_supabase.py on a $2-3/month cloud server
  - Deploy mt5_history.py output there instead
  - More complex, but PC doesn't need to be on

Option A is best for your case (always-on Windows PC).
"""

# ─── MONITORING ────────────────────────────────────────────────────

"""
Check if sync is working:

1. Local testing:
   python -c "from supabase import create_client; import os; \
              c = create_client('https://amykrtiekuyccddwyvbb.supabase.co', \
              'sb_publishable_49Ob7v2rKNuzBaRaNGfhEg_DKoFqUQe'); \
              print(c.table('live_trading_data').select('*').limit(1).execute())"

2. In Streamlit Cloud:
   - Dashboard will show "⚠️ No data in Supabase" if uploader is off
   - Will show live data if uploader is running

3. Check Supabase directly:
   - Open https://app.supabase.com
   - Tables → live_trading_data → inspect data
"""

# ─── TROUBLESHOOTING ───────────────────────────────────────────────

"""
Dashboard shows "No data in Supabase"
  → Check if upload_to_supabase.py is running
  → Check Supabase table exists (see Step 1)
  → Check API key is correct

Upload fails with "Connection error"
  → Check internet connection
  → Check Supabase URL/Key are correct
  → Check firewall isn't blocking Supabase

Streamlit Cloud won't load
  → Check requirements.txt has all dependencies
  → Check env variables are set in Secrets
  → Check dashboard_cloud.py has no local file paths
  → Check logs in Streamlit Cloud (view app logs)

Data is old/not updating
  → Check upload_to_supabase.py is still running
  → Check mt5_history.py is writing scuro_live_data.json
  → Look at terminal output of uploader for errors
"""

# ─── FILES IN THIS SETUP ───────────────────────────────────────────

"""
sir_bane_v1/
├── advisor.py                    (existing)
├── dashboard.py                  (existing - local version)
├── mt5_history.py               (existing)
├── scuro_config.json            (existing)
├── scuro_live_data.json         (existing - generated by mt5_history.py)
│
├── upload_to_supabase.py         (NEW - runs on your PC)
├── dashboard_cloud.py            (NEW - deployed to Streamlit Cloud)
├── requirements.txt              (NEW - dependencies)
└── CLOUD_DEPLOYMENT.txt          (this file)
"""

# ─── QUICK START ────────────────────────────────────────────────────

"""
1. Minute 1: Create table in Supabase (copy-paste SQL)
2. Minute 2: pip install supabase
3. Minute 3: python upload_to_supabase.py (keep running)
4. Minute 10: Create GitHub repo with these files
5. Minute 15: Deploy on Streamlit Cloud
6. Minute 20: Set env variables
7. Minute 25: Your dashboard is LIVE ✨

Total time: ~25 minutes
Cost: FREE (Supabase Free tier, Streamlit Free tier)
"""
