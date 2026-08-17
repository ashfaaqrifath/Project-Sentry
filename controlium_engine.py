import os
import re
import time
import uuid
import logging
import socket
import datetime
import json
import subprocess
import threading
import psutil
import ctypes
import pygame.mixer
import pyperclip
import pyautogui
import telebot
import requests
import winshell
import webbrowser
import pyttsx3
import pygetwindow as gw
import hashlib
import hmac
from dotenv import load_dotenv
from tkinter import Tk, messagebox
from plyer import notification
import screen_brightness_control as scrn
import sys

# Ensure project root is importable when this controller is run standalone
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = SCRIPT_DIR
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

from sentry_audit import make_audit_line, append_audit_line


load_dotenv()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
AUTHORIZED_CHAT_IDS = {
    chat_id.strip()
    for chat_id in os.getenv("TELEGRAM_CHAT_ID", "").split(",")
    if chat_id.strip()
}
command_approval_lock = threading.Lock()
SETTINGS_FILE = os.environ.get(
    "SENTRY_SETTINGS_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json"),
)

# Setup project paths
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = SCRIPT_DIR
USER_LOGS_DIR = os.path.join(PROJECT_DIR, "user logs")
REPORTS_DIR = os.path.join(PROJECT_DIR, "reports")
DASHBOARD_URL = os.environ.get("SENTRY_DASHBOARD_URL", "http://127.0.0.1:8765")


def telegram_alert(send):
    bot_token = BOT_TOKEN
    my_chatID = os.getenv("TELEGRAM_CHAT_ID", "")
    send_text = "https://api.telegram.org/bot" + bot_token + "/sendMessage?chat_id=" + my_chatID + "&parse_mode=Markdown&text=" + send

    response = requests.get(send_text)
    return response.json()

incognito = False

bot = telebot.TeleBot(BOT_TOKEN)
original_reply_to = bot.reply_to


def audit_reply(message, text, *args, **kwargs):
    command_text = (getattr(message, "text", "") or "").strip()
    response_text = str(text or "").strip()
    if command_text:
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # write to sentry audit log file (persisted)
        try:
            append_audit_line(command_text, response_text, source="remote", timestamp=timestamp)
        except Exception:
            pass
        # still print so the overseer captures it in component logs
        print(make_audit_line(command_text, response_text, timestamp=timestamp), flush=True)
    return original_reply_to(message, text, *args, **kwargs)


bot.reply_to = audit_reply


def is_authorized(message):
    return str(message.chat.id) in AUTHORIZED_CHAT_IDS


telegram_authenticated_chats = set()


def telegram_commands_allow_all():
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as settings_file:
            settings = json.load(settings_file)
        return bool(settings.get("telegram_commands_allow_all", False))
    except (OSError, json.JSONDecodeError, AttributeError):
        return False


def read_settings():
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as settings_file:
            return json.load(settings_file)
    except (OSError, json.JSONDecodeError):
        return {}


def verify_dashboard_password(password):
    auth = read_settings().get("dashboard_auth", {})
    try:
        salt = bytes.fromhex(auth["salt"])
        expected_hash = bytes.fromhex(auth["hash"])
    except (KeyError, TypeError, ValueError):
        return False

    actual_hash = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, 100000
    )
    return hmac.compare_digest(actual_hash, expected_hash)


def verify_telegram_password(message):
    chat_id = str(message.chat.id)
    entered_password = (getattr(message, "text", "") or "").strip()
    if verify_dashboard_password(entered_password):
        telegram_authenticated_chats.add(chat_id)
        bot.reply_to(message, "Telegram bot authentication successful.")
    else:
        bot.reply_to(message, "Incorrect password")


def prompt_for_telegram_password(message):
    bot.reply_to(message, "Please dashboard password.")
    bot.register_next_step_handler(message, verify_telegram_password)


def request_command_approval(message):
    command_text = (getattr(message, "text", "") or "").strip()
    sender = getattr(message, "from_user", None)
    sender_name = getattr(sender, "first_name", "") or "Telegram user"
    prompt = (
        f"Remote command received from {sender_name}:\n\n"
        f"{command_text}\n\n"
        "Allow command execution?"
    )

    try:
        with command_approval_lock:
            approval_root = Tk()
            approval_root.withdraw()
            approval_root.attributes("-topmost", True)
            approval_root.lift()
            approval_root.focus_force()
            approval_root.update_idletasks()
            try:
                approved = messagebox.askyesno(
                    "Sentry remote command",
                    prompt,
                    parent=approval_root,
                )
            finally:
                approval_root.destroy()
    except Exception as exc:
        append_audit_line(
            command_text,
            f"REJECTED - local approval popup failed: {exc}",
            source="remote",
        )
        return False

    decision = "APPROVED" if approved else "REJECTED"
    append_audit_line(command_text, f"{decision} - local user permission", source="remote")
    return approved


@bot.message_handler(func=lambda message: True)

def command_unit(message):
    global incognito

    try:
        if not is_authorized(message):
            append_audit_line(
                "telegram",
                f"REJECTED - unauthorized chat_id {message.chat.id}",
                source="remote",
            )
            return

        chat_id = str(message.chat.id)
        if chat_id not in telegram_authenticated_chats:
            prompt_for_telegram_password(message)
            return

        if not telegram_commands_allow_all() and not request_command_approval(message):
            bot.reply_to(message, "Command denied by the local user")
            return

        if message.text.lower() == "/stop":

            bot.reply_to(message, "Engine shutdown")
            subprocess.run(["taskkill", "/F", "/PID", str(pid)])

        elif message.text.lower() == "/log":
            try:
                if os.path.exists(USER_LOGS_DIR):
                    log_files = [f for f in os.listdir(USER_LOGS_DIR) if f.endswith(".txt")]
                    if log_files:
                        latest_log = sorted(log_files)[-1]
                        log_path = os.path.join(USER_LOGS_DIR, latest_log)
                        with open(log_path, 'rb') as file:
                            bot.send_document(message.chat.id, file)
                    else:
                        bot.reply_to(message, "No log files found")
                else:
                    bot.reply_to(message, "Activity logs folder not found")
            except Exception as e:
                bot.reply_to(message, f"Error sending file: {e}")

        elif message.text.lower() == "/report":
            try:
                report_url = f"{DASHBOARD_URL.rstrip('/')}/api/report"
                response = requests.get(report_url, timeout=20)
                if response.status_code == 200 and response.headers.get('Content-Type', '').startswith('application/pdf'):
                    os.makedirs(REPORTS_DIR, exist_ok=True)
                    report_name = f"Sentry-Report-{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
                    report_path = os.path.join(REPORTS_DIR, report_name)
                    with open(report_path, 'wb') as report_file:
                        report_file.write(response.content)
                    with open(report_path, 'rb') as file:
                        bot.send_document(message.chat.id, file)
                else:
                    bot.reply_to(message, f"Failed to generate report: HTTP {response.status_code}")
            except Exception as e:
                bot.reply_to(message, f"Error generating report: {e}")

        elif message.text.lower() == "/ss":
            try:
                os.makedirs(USER_LOGS_DIR, exist_ok=True)
                screenshot = pyautogui.screenshot()
                screenshot_path = os.path.join(USER_LOGS_DIR, "controlium-ss.jpg")
                screenshot.save(screenshot_path)
                bot.reply_to(message, f"Screenshot saved to: {screenshot_path}")
                with open(screenshot_path, 'rb') as file:
                    bot.send_document(message.chat.id, file)
            except Exception as e:
                bot.reply_to(message, f"Error taking screenshot: {e}")

        
        elif message.text.lower() == "/clearlogs":
            try:
                if os.path.exists(USER_LOGS_DIR):
                    all_files = os.listdir(USER_LOGS_DIR)
                    txt_files = [f for f in all_files if f.endswith(".txt")]
                    jpg_files = [f for f in all_files if f.endswith(".jpg")]
                    
                    # Find the most recent txt file (currently being written)
                    if txt_files:
                        latest_txt = sorted(txt_files)[-1]
                        # Delete all other txt files
                        for txt_file in txt_files:
                            if txt_file != latest_txt:
                                os.remove(os.path.join(USER_LOGS_DIR, txt_file))
                    
                    # Delete all jpg files
                    for jpg_file in jpg_files:
                        os.remove(os.path.join(USER_LOGS_DIR, jpg_file))
                    
                    bot.reply_to(message, "Activity logs cleared (current log preserved)")
                else:
                    bot.reply_to(message, "Activity logs folder not found")
            except Exception as e:
                bot.reply_to(message, f"ERROR >> {e}")

        

        elif message.text.lower() == "/notepad":
            os.startfile(f"C:/Users/{username}/AppData/Local/Microsoft/WindowsApps/notepad.exe")
            bot.reply_to(message, "Opened Notepad")

        elif message.text.lower() == "/chrome":
            os.startfile("C:\Program Files\Google\Chrome\Application\chrome.exe")
            bot.reply_to(message, "Opened Chrome")

        elif message.text.lower() == "/vscode":
            os.startfile(f"C:/Users/{username}/AppData/Local/Programs/Microsoft VS Code/Code.exe")
            bot.reply_to(message, "Opened Visual Studio Code")

        elif message.text.lower() == "/word":
            os.startfile("C:/Program Files/Microsoft Office/root\Office16/WINWORD.EXE")
            bot.reply_to(message, "Opened Microsoft Word")

        elif message.text.lower() == "/powerpoint":
            os.startfile("C:/Program Files/Microsoft Office/root/Office16/POWERPNT.EXE")
            bot.reply_to(message, "Opened PowerPoint")

        elif message.text.lower() == "/excel":
            os.startfile("C:/Program Files/Microsoft Office/root/Office16/EXCEL.EXE")
            bot.reply_to(message, "Opened Microsoft Excel")

        elif message.text.lower() == "/edge":
            os.startfile("C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
            bot.reply_to(message, "Opened Microsoft Edge")

        elif message.text.lower() == "/files":
            os.startfile("C:/Windows/explorer.exe")
            bot.reply_to(message, "Opened File Explorer")

        elif message.text.lower() == "/alert":
            bot.reply_to(message, "Enter messeage")

            def win_notification(message):
                msg = message.text
                notification.notify(
                    title="Windows notification",
                    message=msg,
                    app_icon=None,
                    timeout=5,)
                bot.reply_to(message, "Done")
    
            bot.register_next_step_handler(message, win_notification)

        elif message.text.lower() == "/popup":
            bot.reply_to(message, "Enter messeage")

            def popup(message):
                msg = message.text
                messagebox.showwarning("Windows", msg)
                bot.reply_to(message, "Done")
                
            bot.register_next_step_handler(message, popup)

        
        

        

        elif message.text.lower() == "/mute":
            pygame.mixer.music.stop()
            bot.reply_to(message, "Audio stopped")

        elif "volup" in message.text.lower():
            vol = message.text.split()[1]
            vol_level = int(vol)

            for v in range(vol_level):
                pyautogui.press('volumeup')

            bot.reply_to(message, f"Volume increased by {vol_level}")

        elif "voldown" in message.text.lower():
            vol = message.text.split()[1]
            vol_level = int(vol)

            for v in range(vol_level):
                pyautogui.press('volumedown')

            bot.reply_to(message, f"Volume decreased by {vol_level}")

        elif "brightness" in message.text.lower():
            brightness = message.text.split()[1]
            brightness_lvl = int(brightness)

            scrn.set_brightness(brightness_lvl)

            bot.reply_to(message, f"Screen brightness: {brightness_lvl}%")

        elif message.text.lower() == "/getfocus":
            focus_window = gw.getActiveWindow()
            bot.reply_to(message, f"Window in focus: {focus_window.title}")

        elif message.text.lower() == "/getallwin":
            open_windows = gw.getWindowsWithTitle("")
            for window in open_windows:
                telegram_alert(window.title)

        elif message.text.lower() == "/closefocus":
            focus_window = gw.getActiveWindow()
            if focus_window is not None:
                focus_window.close()

            bot.reply_to(message, f"Closed {focus_window.title}")

        elif message.text.lower() == "/closeall":
            open_win = gw.getAllWindows()
            for window in open_win:
                window.close()

            bot.reply_to(message, "Closed all windows")

        elif message.text.lower() == "/enter":
            pyautogui.hotkey('enter')
            bot.reply_to(message, "Done")

        elif message.text.lower() == "/undo":
            pyautogui.hotkey('ctrl', 'z')
            bot.reply_to(message, "Done")

        elif message.text.lower() == "/paste":
            pyautogui.hotkey('ctrl', 'v')
            bot.reply_to(message, "Done")

        elif message.text.lower() == "/delete":
            pyautogui.hotkey('delete')
            bot.reply_to(message, "Done")

        

        elif message.text.lower() == "/signout":
            subprocess.call(["shutdown", "/l"])
            bot.reply_to(message, "System sign out")

        elif message.text.lower() == "/hibernate":
            os.system("shutdown /h")
            bot.reply_to(message, "System hibernation")

        elif message.text.lower() == "/shutdown":
            os.system("shutdown /s /t 30")
            bot.reply_to(message, "System shutdown")

        elif message.text.lower() == "/bin":
            winshell.recycle_bin().empty(confirm=False, show_progress=True, sound=True)
            bot.reply_to(message, "Recycle bin cleared")

        

        

        elif ">" in message.text.lower():
            usr_msg = message.text
            speech_engine(usr_msg)
            bot.reply_to(message, "Done")

        elif message.text.lower() == "/time":
            time = datetime.datetime.now().strftime("%H:%M")
            speech_engine(f"The time is {time}")
            bot.reply_to(message, "Done")


        


        elif "search" in message.text.lower():
            indx = message.text.lower().split().index("search")
            conv = message.text.split()[indx + 1:]
            query = ' '.join([str(item) for item in conv])
            webbrowser.open(f"https://www.google.com/search?q={query}")
            bot.reply_to(message, f"Searching {query}")

        

        else:
            bot.reply_to(message, "Invalid command")
            
    except Exception as e:
        bot.reply_to(message, f"ERROR >> {e}")

###############################################################################################

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









def network_connection():
    result = subprocess.run(["netsh", "wlan", "show", "interfaces"], capture_output=True, text=True)
    output = result.stdout
    ssid_line = [line for line in output.splitlines() if "SSID" in line]

    if ssid_line:
        ssid = ssid_line[0].split(":")[1].strip()
        logging.info(f"Connected to network: {ssid}")
    else:
        logging.info("Not connected to a network")





def telegram_bot():
    while True:
        try:
            telegram_alert(f"System online - {username}")
            for chat_id in AUTHORIZED_CHAT_IDS:
                try:
                    bot.send_message(
                        chat_id,
                        "Please enter dashboard password.",
                    )
                except Exception:
                    pass
            bot.polling()
        except:
            time.sleep(5)

def speech_engine(speak):
    engine = pyttsx3.init("sapi5")
    engine.setProperty("rate", 150)
    voices = engine.getProperty('voices')
    engine.setProperty("voice", voices[0].id)
    engine.say(speak)
    engine.runAndWait()


##########################################################################################

if __name__ == "__main__":
    
    
    network_thread = threading.Thread(target=network_connection) # Thread 3
    
    telegram_bot_thread = threading.Thread(target=telegram_bot) # Thread 6

    
    
    network_thread.start()
    
    telegram_bot_thread.start()

    
    
    network_thread.join()
    
    telegram_bot_thread.join()