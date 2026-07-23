import os
import time
import datetime
import logging
import socket
import uuid
import sys
import psutil
import pygetwindow as gw

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOGS_DIR = os.path.join(BASE_DIR, "user logs")
os.makedirs(LOGS_DIR, exist_ok=True)

# 1. Grab system info
hostname = socket.gethostname()
ip = socket.gethostbyname(hostname)
mac = ':'.join(['{:02x}'.format((uuid.getnode() >> elements) & 0xff) for elements in range(0,2*6,2)][::-1])
username = os.getlogin()
cpu_usage = psutil.cpu_percent(interval=1)
ram = psutil.virtual_memory()
ram_used = ram.used / (1024**3)
ram_available = ram.available / (1024**3)
boot_time = psutil.boot_time()
uptime = datetime.datetime.fromtimestamp(boot_time)
pid = os.getpid()

# 2. Setup logging with timestamp
SESSION_START = time.strftime('%Y%m%d_%H%M%S')
LOG_FILE = os.path.join(LOGS_DIR, f"user_log_{SESSION_START}.txt")
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format='%(asctime)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

logged_windows = set()



def windows_logger():
    global logged_windows
    try:
        open_windows = gw.getWindowsWithTitle("")
        
        # Log newly opened windows
        for window in open_windows:
            if window.title not in logged_windows:
                logging.info(f"Opened : {window.title}")
                logged_windows.add(window.title)

        # Log closed windows
        for title in logged_windows.copy():
            if title not in gw.getAllTitles():
                logging.info(f"Closed : {title}")
                logged_windows.remove(title)
    except:
        pass


def activity_logger():
    # Write the header info once at the top of the file
    time_stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logging.info(f'''CONTROLIUM ENGINE - ACTIVITY LOG
{time_stamp}
<< ACTIVITY LOG >>

> IP Address: {ip}
> MAC Address: {mac}
> Active user: {username}
> CPU Usage: {cpu_usage}%
> RAM Usage: {ram_used:.2f} GB
> Available RAM: {ram_available:.2f} GB
> System uptime: {uptime}
> Process ID: {pid}

''')
    
    # Main loop that checks windows every 120 seconds
    while True:
        windows_logger()       # Check windows
        time.sleep(60)        # Wait 1 minute
        

if __name__ == "__main__":
    print("Activity Logger started...")
    try:
        activity_logger()
    except KeyboardInterrupt:
        print("Activity Logger stopped.")
        sys.exit(0)

