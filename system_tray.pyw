import json
import os
import sys
import urllib.error
import urllib.request
import webbrowser
import socket
from dotenv import load_dotenv
from PySide6.QtCore import Qt, QTimer, QPoint
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QPushButton,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

PORT = 8765

def local_ip_address():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"


load_dotenv()
DASHBOARD_URL = os.environ.get("SENTRY_DASHBOARD_URL", f"http://{local_ip_address()}:{PORT}")
DASHBOARD_TOKEN = os.environ.get("SENTRY_DASHBOARD_TOKEN", "")
COMPONENT_LABELS = {
    "keystroke": "Keystroke dynamics monitoring",
    "mouse": "Mouse dynamics monitoring",
    "network": "Network usage monitoring",
    "drive": "Drive health monitoring",
    "remote": "Telegram Bot controller",
}


def with_token(path):
    separator = "&" if "?" in path else "?"
    return f"{path}{separator}token={DASHBOARD_TOKEN}"


def make_tray_icon(color="#64ffda"):
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor("#0a1118"))
    painter.setPen(QColor(color))
    painter.drawRoundedRect(7, 7, 50, 50, 10, 10)
    painter.setPen(QColor(color))
    painter.setFont(QFont("Segoe UI", 27, QFont.Weight.Bold))
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "S")
    painter.end()
    return QIcon(pixmap)


class SentryTrayStatus(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Sentry")
        self.setWindowIcon(make_tray_icon())
        self.setWindowFlags(Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.resize(340, 360) # Tightened height
        self.setMinimumSize(340, 360)
        self.setMaximumSize(340, 360)

        self.status_labels = {}
        self.connection_label = None
        self.system_status_label = None
        self.drag_position = None

        self.build_ui()
        self.build_tray()
        self.refresh_status()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_status)
        self.timer.start(2500)

    def build_ui(self):
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)

        layout = QVBoxLayout(root)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(0) # Manage all spacing manually with addSpacing

        # 1. Title Row
        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title = QLabel("S E N T R Y")
        title.setObjectName("title")
        
        self.system_status_label = QLabel("● ACTIVE")
        self.system_status_label.setObjectName("systemStatus")
        
        title_row.addWidget(title)
        title_row.addStretch()
        title_row.addWidget(self.system_status_label)
        layout.addLayout(title_row)

        # 2. Subtitle (Immediately below title)
        self.connection_label = QLabel("MONITORING DASHBOARD CONNECTED")
        self.connection_label.setObjectName("connection")
        layout.addWidget(self.connection_label)

        layout.addSpacing(14) # Gap before the status box

        # 3. Status Box
        box = QFrame()
        box.setProperty("class", "statusBox")
        box_layout = QVBoxLayout(box)
        box_layout.setContentsMargins(12, 10, 12, 10)
        box_layout.setSpacing(5)
        for key, label in COMPONENT_LABELS.items():
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(10)
            name_label = QLabel(label)
            name_label.setObjectName("compLabel")
            state_label = QLabel("Active")
            state_label.setProperty("state", "unknown")
            state_label.setMinimumWidth(70)
            state_label.setMaximumWidth(70)
            state_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.status_labels[key] = state_label
            row.addWidget(name_label)
            row.addWidget(state_label)
            box_layout.addLayout(row)
        layout.addWidget(box)

        layout.addSpacing(14) # Gap before buttons

        # 4. Buttons
        btn_grid = QVBoxLayout()
        btn_grid.setContentsMargins(0, 0, 0, 0)
        btn_grid.setSpacing(8)
        
        row1 = QHBoxLayout()
        row1.setSpacing(8)
        open_btn = QPushButton("View Log")
        pause_btn = QPushButton("Pause Monitoring")
        row1.addWidget(open_btn)
        row1.addWidget(pause_btn)
        
        row2 = QHBoxLayout()
        row2.setSpacing(8)
        remote_btn = QPushButton("Stop Remote")
        stop_btn = QPushButton("Stop Monitoring")
        stop_btn.setObjectName("danger")
        row2.addWidget(remote_btn)
        row2.addWidget(stop_btn)
        
        btn_grid.addLayout(row1)
        btn_grid.addLayout(row2)
        layout.addLayout(btn_grid)

        # Signals
        open_btn.clicked.connect(self.open_dashboard)
        pause_btn.clicked.connect(self.pause_monitoring)
        remote_btn.clicked.connect(self.stop_remote)
        stop_btn.clicked.connect(self.shutdown_project)

        self.setStyleSheet(self.stylesheet())

    def build_tray(self):
        self.tray = QSystemTrayIcon(make_tray_icon(), self)
        self.tray.setToolTip("Sentry is active")

        menu = QMenu()
        open_action = QAction("View Log", self)
        show_action = QAction("Show Status", self)
        hide_action = QAction("Hide Status", self)
        stop_action = QAction("Stop Monitoring", self)

        open_action.triggered.connect(self.open_dashboard)
        show_action.triggered.connect(self.show_panel)
        hide_action.triggered.connect(self.hide)
        stop_action.triggered.connect(self.shutdown_project)

        menu.addAction(open_action)
        menu.addAction(show_action)
        menu.addAction(hide_action)
        menu.addSeparator()
        menu.addAction(stop_action)

        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self.tray_activated)
        self.tray.show()

    def tray_activated(self, reason):
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self.show_panel()

    def show_panel(self):
        screen = QApplication.primaryScreen().geometry()
        x = screen.right() - self.width() - 10
        y = screen.bottom() - self.height() - 80
        self.move(x, y)
        self.show()
        self.raise_()
        self.activateWindow()

    def open_dashboard(self):
        # Open the latest sentry audit log if available, otherwise open dashboard
        try:
            from sentry_audit import get_latest_log_path
            path = get_latest_log_path()
            if path:
                os.startfile(path)
                return
        except Exception:
            pass
        webbrowser.open(with_token(DASHBOARD_URL.rstrip("/") + "/"))

    def request_json(self, path):
        with urllib.request.urlopen(with_token(path), timeout=3) as response:
            return json.loads(response.read().decode("utf-8"))

    def refresh_status(self):
        try:
            summary = self.request_json(DASHBOARD_URL.rstrip("/") + "/api/summary")
            components = summary.get("components", {})
            active = sum(1 for state in components.values() if state == "enabled")
            system_active = active > 0
            self.system_status_label.setText("● ACTIVE" if system_active else "● OFFLINE")
            self.system_status_label.setProperty("state", "active" if system_active else "inactive")
            self.repolish(self.system_status_label)
            
            self.connection_label.setText("MONITORING DASHBOARD CONNECTED")
            self.connection_label.setProperty("state", "ok")
            self.repolish(self.connection_label)

            for key, label in self.status_labels.items():
                state = components.get(key, "disabled")
                # Default to 100 so components without training data (like remote) show as Active
                training_pct = 100
                if key in summary and isinstance(summary[key], dict) and "training_percent" in summary[key]:
                    training_pct = int(summary[key]["training_percent"])
                
                if state == "enabled" and training_pct < 100:
                    label.setText("Training")
                    label.setProperty("state", "training")
                elif state == "enabled":
                    label.setText("Active")
                    label.setProperty("state", "on")
                else:
                    label.setText("Inactive")
                    label.setProperty("state", "off")
                self.repolish(label)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            self.connection_label.setText("Waiting for dashboard server")
            self.connection_label.setProperty("state", "bad")
            self.system_status_label.setText("● OFFLINE")
            self.system_status_label.setProperty("state", "inactive")
            self.repolish(self.connection_label)
            self.repolish(self.system_status_label)

    def pause_monitoring(self):
        monitoring_components = ["keystroke", "mouse", "network", "drive"]
        for component in monitoring_components:
            try:
                request = urllib.request.Request(
                    with_token(DASHBOARD_URL.rstrip("/") + "/api/control"),
                    data=json.dumps({"component": component, "action": "stop"}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                urllib.request.urlopen(request, timeout=2).read()
            except Exception:
                pass
        self.refresh_status()

    def stop_remote(self):
        try:
            request = urllib.request.Request(
                with_token(DASHBOARD_URL.rstrip("/") + "/api/control"),
                data=b'{"component": "remote", "action": "stop"}',
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(request, timeout=3).read()
        except (urllib.error.URLError, TimeoutError):
            pass

    def shutdown_project(self):
        try:
            request = urllib.request.Request(
                with_token(DASHBOARD_URL.rstrip("/") + "/api/shutdown"),
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(request, timeout=3).read()
        except (urllib.error.URLError, TimeoutError):
            pass
        QApplication.quit()

    def repolish(self, widget):
        widget.style().unpolish(widget)
        widget.style().polish(widget)

    def focusOutEvent(self, event):
        self.hide()
        event.accept()

    def closeEvent(self, event):
        event.ignore()
        self.hide()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.drag_position = event.globalPosition().toPoint() - self.pos()
        event.accept()

    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.MouseButton.LeftButton and self.drag_position is not None:
            self.move(event.globalPosition().toPoint() - self.drag_position)
        event.accept()

    def stylesheet(self):
        return """
            * {
                font-family: 'Chakra Petch', 'Segoe UI', sans-serif;
            }
            QWidget#root {
                background: #23272d;
                color: #d8e7ee;
                font-size: 10pt;
                border: 1px solid #3b3f45;
                border-radius: 12px;
            }
            QLabel#title {
                color: #64ffda;
                font-size: 16pt;
                font-weight: 900;
                margin: 0;
                padding: 0;
                line-height: 1;
            }
            QLabel#connection {
                color: #8ca0ad;
                font-size: 10pt;
                margin-top: -6px;
                padding: 0;
            }
            QLabel#connection[state="bad"] {
                color: #ff6b7a;
            }
            QFrame[class="statusBox"] {
                background: #1e2125;
                border: 1px solid #3b3f45;
                border-radius: 8px;
            }
            QLabel#compLabel {
                font-weight: 600;
            }
            QLabel[state="on"] {
                color: #64ffda;
                font-weight: 800;
            }
            QLabel[state="training"] {
                color: #ffb84d;
                font-weight: 800;
            }
            QLabel[state="off"], QLabel[state="unknown"] {
                color: #ff6b7a;
                font-weight: 800;
            }
            QLabel#systemStatus {
                color: #d8e7ee;
                font-weight: 800;
                font-size: 10pt;
            }
            QLabel#systemStatus[state="active"] {
                color: #64ffda;
            }
            QLabel#systemStatus[state="inactive"] {
                color: #ff6b7a;
            }
            QPushButton {
                background: rgba(100, 255, 218, 0.12);
                color: #64ffda;
                font-weight: 800;
                font-size: 10pt;
                
                border-radius: 8px;
                padding: 9px 12px;
            }
            QPushButton:hover {
                background: rgba(100, 255, 218, 0.20);
                border: 1px solid rgba(100, 255, 218, 0.22);
            }
            QPushButton#danger {
                background: rgba(255, 107, 122, 0.12);
                color: #ff6b7a;
                border-color: rgba(255, 107, 122, 0.22);
            }
            QPushButton#danger:hover {
                background: rgba(255, 107, 122, 0.20);
                border: 1px solid rgba(255, 107, 122, 0.22);
            }
            QMenu {
                background: #0a1118;
                color: #d8e7ee;
                border: 1px solid #1d3340;
                font-weight: 600;
            }
            QMenu::item:selected {
                background: rgba(100, 255, 218, 0.12);
                color: #64ffda;
            }
        """


def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    window = SentryTrayStatus()
    window.hide()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
