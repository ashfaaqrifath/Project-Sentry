# Project Sentry

Project Sentry is a Windows-focused behavioral monitoring and anomaly detection system for local system telemetry, user activity tracking, and remote bot-based control. It combines:

- Keystroke and mouse behavior analysis
- Network usage anomaly detection
- Drive health monitoring
- Activity logging and event capture
- Telegram alerts and remote operational commands
- A local dashboard for overview and controls

This repository is intended for research, monitoring, and controlled security or operations use on Windows machines where you have permission to run and inspect such activity logging.

## Features

- Behavioral anomaly detection using CSV training data and model files
- Monitoring for keystroke, mouse, network, and drive health patterns
- Local web dashboard for status overview and controls
- Telegram notifications for alerts and system events
- Remote command execution through a Telegram bot
- Session log capture for system and remote actions
- Report generation for detected anomalies and diagnostics

## Project Structure

```text
Project-Sentry/
├── activity_logger.py
├── command_engine.py
├── dashboard.html
├── generate_report.py
├── main.pyw
├── telegram_alert.py
├── settings.json
├── requirements.txt
├── reports/
├── sentry logs/
├── user logs/
├── assets/
├── detection engine/
│   ├── drive health monitor/
│   ├── keystroke dynamics/
│   ├── mouse dynamics/
│   └── network usage/
├── tests/
├── README.md
├── LICENSE.txt
└── .env
```

## Requirements

- Windows 10 or Windows 11
- Python 3.10+
- A Telegram bot token and authorized chat ID
- Access to the machine where the monitoring app will run

## Important behavior model note

This project is based on behavioral biometrics. The data it learns from is unique to each person and each machine. A user’s keystroke rhythm, mouse motion patterns, network behavior, and drive activity are not universal features—they vary by hardware, habits, posture, environment, and usage patterns.

Because of that, the anomaly detectors must be trained on the target person’s normal behavior before they can reliably detect deviations. The CSV files in the `detection engine` folders are baseline datasets used to build the model for that specific user profile. If the model is trained on someone else’s behavior, it may produce false positives or miss real anomalies.

### Training concept

The project uses the training files such as:

- `keystroke_dynamics_training.csv`
- `mouse_dynamics_training.csv`
- `network_usage_training.csv`
- `drive_health_training.csv`

These files represent normal, accepted behavior for that user. The app then learns a baseline pattern from them and later compares new activity against that baseline. An anomaly is triggered when the new behavior drifts too far from the trained baseline.

### What this means in practice

- The model should be trained on the actual person and machine you plan to monitor.
- The training data must be collected during normal, non-abnormal activity.
- If the user’s behavior changes significantly over time, retraining may be needed.
- The detectors are best used as a personalized baseline system, not a universal one-size-fits-all detector.

## Setup

### 1) Clone the repository

```powershell
git clone https://github.com/ashfaaqrifath/Project-Sentry.git
cd Project-Sentry
```

### 2) Create and activate a virtual environment

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

### 3) Install dependencies

```powershell
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

#### Manual install option

```powershell
pip install pyautogui pyperclip psutil python-dotenv plyer winshell pyttsx3 screen-brightness-control pygame pyTelegramBotAPI requests pygetwindow pynput numpy pandas scikit-learn joblib matplotlib pytest
```

### 4) Create your environment file

Create a `.env` file in the project root:

```env
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_CHAT_ID=your_chat_id_here
SENTRY_DASHBOARD_TOKEN=your_dashboard_token_here
```

#### How to get each value

1. Dashboard token
   - When the dashboard starts, it generates a token automatically if `SENTRY_DASHBOARD_TOKEN` is missing.
   - You can also set a custom value manually in `.env`.
   - Example:

   ```env
   SENTRY_DASHBOARD_TOKEN=my_secure_dashboard_token
   ```

   - Use the same value in the dashboard requests if needed by your deployment.

2. Telegram bot token
   - Open Telegram and search for `@BotFather`.
   - Send `/newbot`.
   - Follow the prompts to name your bot and choose a username.
   - Copy the HTTP API token that BotFather gives you.
   - Paste that value into `TELEGRAM_BOT_TOKEN`.

3. Telegram chat ID
   - Start a chat with your bot in Telegram.
   - Send a message like `/start` to the bot.
   - Open this URL in your browser, replacing `YOUR_BOT_TOKEN` with the token you just created:

   ```text
   https://api.telegram.org/botYOUR_BOT_TOKEN/getUpdates
   ```

   - Find the `"chat":{"id":1234567890...}` entry in the JSON output.
   - Use that numeric value for `TELEGRAM_CHAT_ID`.
   - If more than one user should be allowed, separate IDs with commas:

   ```env
   TELEGRAM_CHAT_ID=1234567890,987654321
   ```

#### Example final `.env`

```env
TELEGRAM_BOT_TOKEN=123456:ABCDEF
TELEGRAM_CHAT_ID=1234567890
SENTRY_DASHBOARD_TOKEN=secure_dashboard_token_here
```

> Keep `.env` private and never commit it to GitHub.

### 5) Configure the dashboard password

The dashboard stores a password in `settings.json` on first launch. If needed, set `DASHBOARD_PASSWORD` before starting the app or enter the password when prompted.

### 6) Review runtime settings

`settings.json` contains options such as:

```json
{
  "components": {},
  "telegram_alerts_enabled": true,
  "telegram_commands_allow_all": true,
  "behavior_alerts": {
    "window_seconds": 45,
    "combined_threshold": 0.12
  }
}
```

## Running the Application

### Launch the monitoring dashboard and local app

From the project root:

```powershell
python .\main.pyw
```

This starts the main local dashboard and monitoring components.

### Run the Telegram remote control bot only

```powershell
python .\command_engine.py
```

This starts the bot handler that listens for approved Telegram commands and performs local actions.

## Remote Telegram Commands

The bot accepts commands from authorized chat IDs only. The user may be asked to verify the dashboard password before a command runs.

| Command | Description |
| --- | --- |
| `/stop` | Requests Sentry shutdown through the dashboard |
| `/log` | Sends the latest generated log file |
| `/report` or `/generate report` | Sends a generated PDF system report |
| `/enable all` | Starts all monitored components |
| `/disable all` | Stops all monitored components |
| `/enable <component>` | Starts a component: `keystroke`, `mouse`, `network`, `drive`, `activity`, `remote`, `telegram` |
| `/disable <component>` | Stops a component |
| `/delete detections` | Removes anomaly detections |
| `/delete logs` or `/delete log files` | Deletes generated log files |
| `/clearlogs` | Keeps only the newest log file |
| `/open <app>` | Opens an application such as `notepad`, `calculator`, `chrome`, or `vscode` |
| `/close <app>` | Closes a running app |
| `/alert` | Prompts for a Windows notification message |
| `/popup` | Displays a Windows pop-up message |
| `/speak` | Speaks a custom message via the system TTS engine |
| `/mute` | Stops system audio |
| `volup <number>` | Increases system volume |
| `voldown <number>` | Decreases system volume |
| `brightness <number>` | Sets screen brightness |
| `/getfocus` | Returns the currently focused window |
| `/getallwin` | Lists open windows |
| `/closefocus` | Closes the active window |
| `/closeall` | Closes all visible windows |
| `/signout` | Signs the current user out |
| `/lock` | Locks the workstation |
| `/hibernate` | Hibernates the machine |
| `/shutdown` | Schedules a Windows shutdown |
| `/bin` | Empties the Recycle Bin |
| `search <query>` | Opens a Google search for the query |

### Remote command behavior

- Only authorized Telegram chat IDs can send commands.
- Dashboard password verification may be required before execution.
- The app may request local approval when `telegram_commands_allow_all` is disabled.
- Commands can trigger local actions like opening apps, closing windows, locking the machine, and generating reports.

## GitHub repo setup notes

Before pushing to GitHub, make sure the following are ignored:

```gitignore
.env
.venv/
__pycache__/
*.pyc
reports/
user logs/
sentry logs/
*.pkl
```

## Security and Privacy Notice

This project monitors user activity and system behavior, stores logs locally, and can perform remote control actions on a Windows machine. Use it only in environments where such monitoring is permitted, authorized, and compliant with local privacy and security requirements.
