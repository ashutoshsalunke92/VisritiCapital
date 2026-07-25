"""
Run this FIRST, before run_backtest.py, any time something's not working:

    python check_setup.py

It checks every step of setup in order and tells you exactly what's wrong
and exactly what command fixes it. No guessing.
"""
import os
import sys

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(THIS_DIR, ".env")
ENV_EXAMPLE_PATH = os.path.join(THIS_DIR, ".env.example")

ok_count = 0
fail_count = 0


def ok(msg):
    global ok_count
    ok_count += 1
    print(f"  [OK] {msg}")


def fail(msg, fix):
    global fail_count
    fail_count += 1
    print(f"  [FAIL] {msg}")
    print(f"         FIX: {fix}")


print(f"Script folder: {THIS_DIR}")
print()

# ---- 1. Are we in the right folder? ----
print("1. Checking project files are present...")
required_files = ["config.py", "upstox_client.py", "data_fetcher.py",
                   "strategy.py", "backtest_engine.py", "run_backtest.py",
                   "pnl_format.py"]
missing = [f for f in required_files if not os.path.isfile(os.path.join(THIS_DIR, f))]
if missing:
    fail(f"Missing project files: {missing}",
         "You may have run this from the wrong folder, or the download was incomplete. "
         "Re-download and extract the full project zip, then cd into that exact folder.")
else:
    ok("All project files found next to this script.")

# ---- 2. Does .env exist? ----
print("\n2. Checking .env file...")
if not os.path.isfile(ENV_PATH):
    fail(f".env does NOT exist at {ENV_PATH}",
         f'Run:  Copy-Item "{ENV_EXAMPLE_PATH}" "{ENV_PATH}"   then edit it (see step 3).')
else:
    ok(f".env exists at {ENV_PATH}")

    # ---- 3. Does .env have a real-looking token? ----
    print("\n3. Checking .env contents...")
    with open(ENV_PATH, "r", encoding="utf-8-sig", errors="replace") as f:
        content = f.read()

    token_line = None
    for line in content.splitlines():
        if line.strip().startswith("UPSTOX_ACCESS_TOKEN"):
            token_line = line
            break

    if token_line is None:
        fail("No UPSTOX_ACCESS_TOKEN line found in .env at all.",
             f'Open .env in Notepad and add a line: UPSTOX_ACCESS_TOKEN=your_real_token '
             f'(copy from .env.example as a template: notepad "{ENV_PATH}")')
    else:
        value = token_line.split("=", 1)[1].strip() if "=" in token_line else ""
        if value == "" or value == "your_access_token_here":
            fail("UPSTOX_ACCESS_TOKEN is present but still set to the placeholder / empty value.",
                 f'Open .env in Notepad: notepad "{ENV_PATH}"  and replace it with your real '
                 f'token from developer.upstox.com -> your app -> Generate. No quotes, no spaces.')
        elif value.startswith('"') or value.startswith("'"):
            fail(f"UPSTOX_ACCESS_TOKEN has quote characters around it: {value[:20]}...",
                 f'Open .env in Notepad: notepad "{ENV_PATH}"  and remove the quote marks. '
                 f'Line should look exactly like: UPSTOX_ACCESS_TOKEN=eyJ0eXAiOiJKV1Qi...')
        elif len(value) < 50:
            fail(f"UPSTOX_ACCESS_TOKEN looks too short ({len(value)} chars) to be a real token "
                 f"(real Upstox tokens are usually 150+ characters).",
                 "The token may have been cut off when copying. Go back to developer.upstox.com, "
                 "generate a new token, and carefully select/copy the ENTIRE string.")
        else:
            ok(f"UPSTOX_ACCESS_TOKEN found, looks well-formed ({len(value)} chars).")

# ---- 4. Are dependencies installed? ----
print("\n4. Checking Python dependencies...")
for pkg in ["dotenv", "requests", "pandas", "colorama"]:
    try:
        __import__(pkg)
        ok(f"{pkg} is installed.")
    except ImportError:
        fail(f"{pkg} is NOT installed in this Python environment.",
             "Run:  pip install -r requirements.txt   "
             "(make sure your venv is activated first — prompt should show '(venv)')")

# ---- 5. Actually try loading config.py the same way run_backtest.py does ----
print("\n5. Trying to actually load your credentials...")
try:
    sys.path.insert(0, THIS_DIR)
    from config import load_upstox_creds
    creds = load_upstox_creds()
    ok(f"Credentials loaded successfully. Token starts with: {creds.access_token[:15]}...")
except Exception as e:
    fail(f"config.load_upstox_creds() raised: {e}",
         "See the specific FAIL messages above — fixing those will fix this too.")

# ---- Summary ----
print("\n" + "=" * 60)
if fail_count == 0:
    print(f"All {ok_count} checks passed. Try running run_backtest.py now.")
else:
    print(f"{fail_count} problem(s) found, {ok_count} check(s) passed.")
    print("Fix the FAIL items above in order (top to bottom), then run "
          "check_setup.py again to confirm before trying run_backtest.py.")
print("=" * 60)
