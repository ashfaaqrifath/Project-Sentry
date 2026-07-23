# Project Sentry

Project Sentry is a Windows-focused behavioral monitoring and anomaly detection system. It combines local activity tracing, multi-modal telemetry monitoring, a lightweight dashboard, and Telegram-based alerting/remote control features.

## Overview

This project is designed to monitor and detect suspicious or abnormal system usage patterns across several categories:

- Keystroke dynamics
- Mouse dynamics
- Network usage
- Drive health
- User activity / window usage

The application stores training and detection data in CSV files, writes session logs in plain text, and exposes a small dashboard interface for visibility.

## Project Structure

- `main.pyw` – main desktop entry point
- `controlium_engine.py` – remote controller / command execution engine
- `sentry_audit.py` – audit log helpers
- `telegram_alert.py` – Telegram notification utility
- `user_activity_logger.py` – system activity logging
- `dashboard.html` – web dashboard front-end
- `settings.json` – runtime settings
- `keystroke dynamics/` – keystroke dataset and monitor
- `mouse dynamics/` – mouse dataset and monitor
- `network usage/` – network dataset and monitor
- `drive health monitor/` – drive health dataset and monitor

## Features

- Behavioral anomaly detection using training datasets
- Local dashboard interface for status monitoring
- Telegram notifications for detected anomalies
- Remote command support through a Telegram bot controller
- Session audit logs for system command and event tracking
- Cross-component monitoring for user and system behavior

## Requirements

- Windows 10/11
- Python 3.10+
- A Telegram bot token and chat ID for alerting / remote control

## Recommended Setup

1. Open a terminal in the project folder.
2. Create and activate a virtual environment:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

3. Install the required Python packages:

```powershell
pip install python-dotenv psutil pyautogui pygetwindow pyperclip requests plyer winshell pyttsx3 screen-brightness-control pygame telebot
```

4. Create a `.env` file in the project root with the following variables:

```env
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
SENTRY_DASHBOARD_TOKEN=your_dashboard_token
```

> Keep your `.env` file private and never commit it to Git.

## Running the Project

From the project root:

```powershell
python .\main.pyw
```

If you want to run the remote control engine directly:

```powershell
python .\controlium_engine.py
```

## Important Notes

- This project relies on Windows-specific libraries and paths.
- Generated log folders such as `sentry logs/`, `user logs/`, and `activity logs/` should not normally be committed to Git.
- The training and anomaly detection CSV files are part of the workspace and may need to be refreshed or retrained for your environment.

## Security / Privacy Notice

This project collects user/system telemetry and writes local logs. Use it only in environments where such monitoring is authorized and compliant with local security and privacy requirements.

## GitHub Push Guide

If you already have an existing GitHub repository and want to push this folder into it:

```powershell
git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/<your-username>/<your-repo-name>.git
git push -u origin main
```

If the repo already has a remote configured, use:

```powershell
git remote set-url origin https://github.com/<your-username>/<your-repo-name>.git
git push -u origin main
```

## License

This project is distributed under the terms in `LICENSE.txt`.
