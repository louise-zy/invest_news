import time
import schedule
import subprocess
import os
from datetime import datetime

SCRIPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "esdm_monitor.py")

def job():
    print(f"\n[Scheduler] Running job at {datetime.now()}")
    try:
        # Run the monitor script
        subprocess.run(["python", SCRIPT_PATH], check=True)
    except subprocess.CalledProcessError as e:
        print(f"[Scheduler] Job failed: {e}")
    except Exception as e:
        print(f"[Scheduler] Error: {e}")

# Schedule every 10 minutes
schedule.every(10).minutes.do(job)

if __name__ == "__main__":
    print("[Scheduler] Starting ESDM Monitor Scheduler (every 10 mins)...")
    print("[Scheduler] Press Ctrl+C to stop.")
    
    # Run immediately once
    job()
    
    while True:
        schedule.run_pending()
        time.sleep(1)
