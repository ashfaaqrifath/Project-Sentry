import os
import io
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

username = os.getlogin()
                                                                
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = SCRIPT_DIR
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

from activity_logger import make_activity_line, append_activity_line


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


def activity_reply(message, text, *args, **kwargs):
    command_text = (getattr(message, "text", "") or "").strip()
    response_text = str(text or "").strip()
    if command_text:
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                                                    
        try:
            append_activity_line(command_text, response_text, source="remote", timestamp=timestamp)
        except Exception:
            pass
                                                                   
        print(make_activity_line(command_text, response_text, timestamp=timestamp), flush=True)
    return original_reply_to(message, text, *args, **kwargs)


bot.reply_to = activity_reply


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
        append_activity_line(
            command_text,
            f"REJECTED - local approval popup failed: {exc}",
            source="remote",
        )
        return False

    decision = "APPROVED" if approved else "REJECTED"
    append_activity_line(command_text, f"{decision} - local user permission", source="remote")
    return approved


def dashboard_request(path, method="POST", payload=None):
    url = f"{DASHBOARD_URL.rstrip('/')}{path}"
    headers = {"X-Sentry-Token": os.getenv("SENTRY_DASHBOARD_TOKEN", "")}
    response = requests.request(method, url, json=payload or {}, headers=headers, timeout=20)
    response.raise_for_status()
    return response


def remote_component_action(message, component, action):
    if component == "telegram":
        dashboard_request("/api/settings", payload={"telegram_alerts_enabled": action == "start"})
        bot.reply_to(message, f"TELEGRAM ALERTS {action.upper()} request sent.")
        return
    valid_components = {"keystroke", "mouse", "network", "drive", "activity", "remote"}
    if component not in valid_components:
        bot.reply_to(message, f"Unknown component. Use: {', '.join(sorted(valid_components))}")
        return
    dashboard_request("/api/control", payload={"component": component, "action": action})
    bot.reply_to(message, f"{component.upper()} {action.upper()} request sent.")


def remote_all_components(message, action):
    dashboard_request("/api/control-all", payload={"action": action})
    dashboard_request("/api/settings", payload={"telegram_alerts_enabled": action == "start"})
    bot.reply_to(message, f"ALL COMPONENTS {action.upper()} request sent.")


def send_system_report(message):
    response = dashboard_request("/api/report", method="GET")
    report = io.BytesIO(response.content)
    report.name = f"Sentry-Report-{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
    bot.send_document(message.chat.id, report)


def clear_detections(message):
    dashboard_request("/api/delete-anomalies")
    bot.reply_to(message, "All anomaly detections deleted.")


def clear_log_files(message):
    dashboard_request("/api/delete-log-dirs")
    bot.reply_to(message, "User and Sentry log files deleted.")


APP_ALIASES = {
    "notepad": "notepad",
    "calculator": "calc",
    "calc": "calc",
    "file explorer": "explorer",
    "explorer": "explorer",
    "command prompt": "cmd",
    "cmd": "cmd",
    "powershell": "powershell",
    "task manager": "taskmgr",
    "resource monitor": "resmon",
    "control panel": "control",
    "paint": "mspaint",
    "snipping tool": "snippingtool",
    "character map": "charmap",
    "on screen keyboard": "osk",
    "magnifier": "magnify",
    "registry editor": "regedit",
    "system information": "msinfo32",
    "system configuration": "msconfig",
    "disk management": "diskmgmt.msc",
    "device manager": "devmgmt.msc",
    "services": "services.msc",
    "event viewer": "eventvwr.msc",
    "windows media player": "wmplayer",
    "media player": "wmplayer",
    "edge": "msedge",
    "microsoft edge": "msedge",
    "chrome": "chrome",
    "google chrome": "chrome",
    "firefox": "firefox",
    "brave": "brave",
    "opera": "opera",
    "word": "winword",
    "microsoft word": "winword",
    "excel": "excel",
    "microsoft excel": "excel",
    "powerpoint": "powerpnt",
    "microsoft powerpoint": "powerpnt",
    "outlook": "outlook",
    "microsoft outlook": "outlook",
    "teams": "ms-teams",
    "microsoft teams": "ms-teams",
    "vscode": "code",
    "visual studio code": "code",
    "spotify": "spotify",
}


def application_executable(app_name):
    normalized = " ".join(app_name.strip().lower().split())
    executable = APP_ALIASES.get(normalized, normalized)
    return executable if executable.endswith(".exe") else f"{executable}.exe"


def open_application(app_name):
    executable = application_executable(app_name)
    if executable == ".exe":
        return False
    if sys.platform == "win32":
        os.startfile(executable)
    else:
        subprocess.Popen([executable])
    return True


def close_application(app_name):
    target_name = application_executable(app_name).lower()
    matches = []
    for process in psutil.process_iter(["name"]):
        try:
            if (process.info.get("name") or "").lower() == target_name:
                matches.append(process)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    for process in matches:
        try:
            process.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return len(matches)


@bot.message_handler(func=lambda message: True)

def command_unit(message):
    global incognito

    try:
        if not is_authorized(message):
            append_activity_line(
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

        command = (message.text or "").strip().lower()

        if command == "/stop":
            bot.reply_to(message, "Sentry shutdown requested")
            dashboard_request("/api/shutdown")

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

        elif command in ("/report", "/generate report"):
            send_system_report(message)

        elif command == "/enable all":
            remote_all_components(message, "start")

        elif command == "/disable all":
            remote_all_components(message, "stop")

        elif command.startswith("/enable "):
            remote_component_action(message, command.split(maxsplit=1)[1], "start")

        elif command.startswith("/disable "):
            remote_component_action(message, command.split(maxsplit=1)[1], "stop")

        elif command == "/delete detections":
            clear_detections(message)

        elif command in ("/delete logs", "/delete log files"):
            clear_log_files(message)

        elif command == "/clearlogs":
            try:
                if os.path.exists(USER_LOGS_DIR):
                    all_files = os.listdir(USER_LOGS_DIR)
                    txt_files = [f for f in all_files if f.endswith(".txt")]
                    

                    if txt_files:
                        latest_txt = sorted(txt_files)[-1]
                                                    
                        for txt_file in txt_files:
                            if txt_file != latest_txt:
                                os.remove(os.path.join(USER_LOGS_DIR, txt_file))
                    
                    bot.reply_to(message, "Activity logs cleared")
                else:
                    bot.reply_to(message, "Activity logs folder not found")
            except Exception as e:
                bot.reply_to(message, f"ERROR >> {e}")

        

        elif command.startswith("/open "):
            app_name = command.split(maxsplit=1)[1].strip()
            try:
                open_application(app_name)
                bot.reply_to(message, f"Opening {app_name}")
            except (FileNotFoundError, OSError) as exc:
                bot.reply_to(message, f"Could not open {app_name}: {exc}")

        elif command.startswith("/close "):
            app_name = command.split(maxsplit=1)[1].strip()
            closed_count = close_application(app_name)
            bot.reply_to(message, f"Closed {app_name}" if closed_count else f"No running {app_name} app found")

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

        elif message.text.lower() == "/speak":
            bot.reply_to(message, "Enter what Sentry should say")

            def speak_message(message):
                speech_engine(message.text)
                bot.reply_to(message, "Done")

            bot.register_next_step_handler(message, speak_message)


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
            window_names = [window.title.strip() for window in open_windows if window.title.strip()]
            if window_names:
                response = "Open windows:\n" + "\n".join(f"- {name}" for name in window_names)
                bot.reply_to(message, response[:4096])
            else:
                bot.reply_to(message, "No open windows found")

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



        elif message.text.lower() == "/signout":
            subprocess.call(["shutdown", "/l"])
            bot.reply_to(message, "System sign out")

        elif message.text.lower() == "/lock":
            if sys.platform == "win32" and ctypes.windll.user32.LockWorkStation():
                bot.reply_to(message, "Workstation locked")
            else:
                bot.reply_to(message, "Could not lock workstation")

        elif message.text.lower() == "/hibernate":
            os.system("shutdown /h")
            bot.reply_to(message, "System hibernation")

        elif message.text.lower() == "/shutdown":
            os.system("shutdown /s /t 30")
            bot.reply_to(message, "System shutdown")

        elif message.text.lower() == "/bin":
            winshell.recycle_bin().empty(confirm=False, show_progress=True, sound=True)
            bot.reply_to(message, "Recycle bin cleared")



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


if __name__ == "__main__":
    
    network_thread = threading.Thread(target=network_connection)           
    telegram_bot_thread = threading.Thread(target=telegram_bot)           
    network_thread.start()
    telegram_bot_thread.start()
    network_thread.join()
    telegram_bot_thread.join()