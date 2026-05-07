#!/usr/bin/env python3
"""
auto_push_github.py  ─  Automated Git Commit & Push
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Automatically commits and pushes code to GitHub without manual steps.

Usage:
    python auto_push_github.py "Commit message here"
    python auto_push_github.py  # Uses default message

Or set up automation:
    python auto_push_github.py --auto  # Runs every 5 minutes
"""

import os
import sys
import subprocess
import time
from datetime import datetime
import json

# ─── CONFIG ──────────────────────────────────────────────
from dotenv import load_dotenv

load_dotenv()

REPO_OWNER = os.getenv("REPO_OWNER", "sirebane")
REPO_NAME = os.getenv("REPO_NAME", "sirbanexau")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
BRANCH = os.getenv("BRANCH", "main")
AUTO_INTERVAL = 300  # 5 minutes

# Files to track (optional - auto-detects all changes if empty)
FILES_TO_COMMIT = [
    "upload_to_supabase.py",
    "dashboard_cloud.py",
    "requirements.txt",
    "CLOUD_DEPLOYMENT.md",
    "QUICK_START.txt",
    "scuro_config.json",
]


def run_command(cmd: str, description: str = "") -> tuple[bool, str]:
    """Execute shell command and return success/output."""
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        success = result.returncode == 0
        output = result.stdout + result.stderr
        
        if description:
            status = "✓" if success else "✗"
            print(f"{status} {description}")
        
        if not success and result.stderr:
            print(f"  Error: {result.stderr.strip()}")
        
        return success, output
    except subprocess.TimeoutExpired:
        print(f"✗ {description} (timeout)")
        return False, ""
    except Exception as e:
        print(f"✗ {description} ({e})")
        return False, ""


def check_git_installed():
    """Verify git is installed."""
    success, _ = run_command("git --version")
    if not success:
        print("✗ Git is not installed. Install from https://git-scm.com/")
        sys.exit(1)
    print("✓ Git is installed")


def initialize_repo():
    """Initialize git repo if not already initialized."""
    if os.path.exists(".git"):
        print("✓ Git repository already initialized")
        return True
    
    print("\n⚠ Git repository not found. Initializing...")
    success, _ = run_command("git init", "Initialize git repo")
    if not success:
        return False
    
    # Set user config (required for commits)
    run_command(
        f'git config user.email "trading-bot@example.com"',
        "Set git user email"
    )
    run_command(
        f'git config user.name "Trading Bot"',
        "Set git user name"
    )
    
    # Add remote
    if GITHUB_TOKEN:
        remote_url = f"https://{GITHUB_TOKEN}@github.com/{REPO_OWNER}/{REPO_NAME}.git"
    else:
        remote_url = f"https://github.com/{REPO_OWNER}/{REPO_NAME}.git"
    
    success, _ = run_command(
        f'git remote add origin "{remote_url}"',
        "Add GitHub remote"
    )
    
    if not success:
        print("✗ Failed to add remote. Make sure the GitHub repo exists.")
        print(f"  URL: https://github.com/{REPO_OWNER}/{REPO_NAME}")
        return False
    
    return True


def get_git_status():
    """Check what files have changed."""
    success, output = run_command("git status --short", "Check git status")
    if not success:
        return []
    return output.strip().split("\n") if output.strip() else []


def commit_and_push(message: str):
    """Stage, commit, and push all changes."""
    print(f"\n{'='*60}")
    print(f"PUSHING TO GITHUB")
    print(f"{'='*60}\n")
    
    # Check for changes
    status = get_git_status()
    if not status:
        print("✓ No changes to commit")
        return True
    
    print(f"Found {len(status)} changed files:")
    for line in status[:10]:  # Show first 10
        print(f"  {line}")
    if len(status) > 10:
        print(f"  ... and {len(status) - 10} more")
    
    # Stage files
    if FILES_TO_COMMIT:
        for file in FILES_TO_COMMIT:
            run_command(f'git add "{file}"', f"Stage {file}")
    else:
        success, _ = run_command("git add .", "Stage all changes")
        if not success:
            return False
    
    # Commit
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    commit_msg = f"{message} ({timestamp})" if message else f"Auto-sync trading data ({timestamp})"
    
    success, _ = run_command(
        f'git commit -m "{commit_msg}"',
        f'Commit: "{commit_msg}"'
    )
    
    if not success:
        print("✗ Commit failed (likely no changes)")
        return False
    
    # Push
    success, output = run_command(
        f"git push -u origin {BRANCH}",
        f"Push to GitHub ({BRANCH} branch)"
    )
    
    if success:
        print(f"\n✓ Successfully pushed to GitHub!")
        print(f"  Repository: https://github.com/{REPO_OWNER}/{REPO_NAME}")
        return True
    else:
        print(f"\n✗ Push failed. Check:")
        print(f"  1. GitHub credentials (GITHUB_TOKEN env var)")
        print(f"  2. Repository exists: https://github.com/{REPO_OWNER}/{REPO_NAME}")
        print(f"  3. Internet connection")
        return False


def auto_push_loop():
    """Continuously push changes at intervals."""
    print(f"\n{'='*60}")
    print(f"AUTO-PUSH ENABLED")
    print(f"{'='*60}")
    print(f"Pushing every {AUTO_INTERVAL} seconds ({AUTO_INTERVAL/60:.0f} minutes)")
    print(f"Press Ctrl+C to stop\n")
    
    iteration = 0
    try:
        while True:
            iteration += 1
            timestamp = datetime.now().strftime("%H:%M:%S")
            print(f"\n[{timestamp}] Auto-push iteration #{iteration}")
            
            commit_and_push("Auto-sync")
            
            print(f"Waiting {AUTO_INTERVAL}s until next push...")
            time.sleep(AUTO_INTERVAL)
    except KeyboardInterrupt:
        print("\n\n✓ Auto-push stopped")


def setup_github_credentials():
    """Guide user through setting up GitHub credentials."""
    print("\n" + "="*60)
    print("GITHUB AUTHENTICATION SETUP")
    print("="*60)
    
    print("\nOption 1: Personal Access Token (Recommended)")
    print("  1. Go to https://github.com/settings/tokens")
    print("  2. Click 'Generate new token' → 'Generate new token (classic)'")
    print("  3. Name: 'sir_bane_v1_auto_push'")
    print("  4. Select scopes: 'repo' (full control)")
    print("  5. Generate & copy token")
    print("  6. Set environment variable:")
    print("     $env:GITHUB_TOKEN = 'ghp_xxxxxxxxxxxxx'")
    print("     (in PowerShell)")
    
    print("\nOption 2: SSH Key")
    print("  1. Generate key: ssh-keygen -t ed25519 -C 'your_email@example.com'")
    print("  2. Add to GitHub: https://github.com/settings/keys")
    print("  3. Update remote: git remote set-url origin git@github.com:USER/REPO.git")
    
    print("\nOption 3: HTTPS with Git Credentials Manager")
    print("  1. Install Git Credentials Manager")
    print("  2. First push will prompt for GitHub login")
    print("  3. Credentials cached automatically\n")


def main():
    print(f"\n{'='*60}")
    print(f"SCURO AUTO-PUSH TO GITHUB")
    print(f"{'='*60}\n")
    
    # Check if username is set
    if REPO_OWNER == "your-username":
        print("⚠ ERROR: Update REPO_OWNER in this script!")
        print("  Line: REPO_OWNER = 'your-username'")
        print("  Change to your actual GitHub username\n")
        return
    
    # Check git
    check_git_installed()
    
    # Initialize repo if needed
    if not initialize_repo():
        setup_github_credentials()
        return
    
    # Parse arguments
    auto_mode = "--auto" in sys.argv
    message = " ".join([arg for arg in sys.argv[1:] if arg != "--auto"]) or "Update trading bot"
    
    if auto_mode:
        auto_push_loop()
    else:
        commit_and_push(message)


if __name__ == "__main__":
    main()
