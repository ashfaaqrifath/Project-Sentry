import json
import os
import sys
import urllib.error
import urllib.request
import webbrowser

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


load_dotenv()
DASHBOARD_URL = os.environ.get("SENTRY_DASHBOARD_URL", "http://127.0.0.1:8765/")
DASHBOARD_TOKEN = os.environ.get("SENTRY_DASHBOARD_TOKEN", "")
COMPONENT_LABELS = {
    "keystroke": "Keystroke dynamics monitoring",
    "mouse": "Mouse dynamics monitoring",
    "drive": "Drive health monitoring",
    "remote": "Network pattern monitoring",
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
        self.resize(340, 350)
        self.setMinimumSize(340, 350)
        self.setMaximumSize(340, 350)

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
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)

        title_layout = QHBoxLayout()
        title = QLabel("Sentry")
        title.setObjectName("title")
        self.system_status_label = QLabel("● Online")
        self.system_status_label.setObjectName("systemStatus")
        self.system_status_label.setMaximumWidth(90)
        title_layout.addWidget(title)
        title_layout.addStretch()
        title_layout.addWidget(self.system_status_label)
        title_widget = QWidget()
        title_widget.setLayout(title_layout)
        layout.addWidget(title_widget)
        
        self.connection_label = QLabel("Control Center connected")
        self.connection_label.setObjectName("connection")

        box = QFrame()
        box.setProperty("class", "statusBox")
        box_layout = QVBoxLayout(box)
        box_layout.setContentsMargins(10, 6, 10, 6)
        box_layout.setSpacing(3)
        for key, label in COMPONENT_LABELS.items():
            row = QHBoxLayout()
            row.setSpacing(8)
            name_label = QLabel(label)
            name_label.setMinimumWidth(60)
            state_label = QLabel("Running")
            state_label.setProperty("state", "unknown")
            state_label.setMinimumWidth(70)
            state_label.setMaximumWidth(70)
            self.status_labels[key] = state_label
            row.addWidget(name_label)
            row.addStretch()
            row.addWidget(state_label)
            box_layout.addLayout(row)
        layout.addWidget(box)

        open_btn = QPushButton("Open Dashboard")
        pause_btn = QPushButton("Pause Monitoring")
        remote_btn = QPushButton("Stop Remote")
        stop_btn = QPushButton("Stop Monitoring")
        stop_btn.setObjectName("danger")
        open_btn.clicked.connect(self.open_dashboard)
        pause_btn.clicked.connect(self.pause_monitoring)
        remote_btn.clicked.connect(self.stop_remote)
        stop_btn.clicked.connect(self.shutdown_project)

        layout.addWidget(self.connection_label)
        btn_layout = QVBoxLayout()
        btn_layout.setSpacing(8)
        top_row = QHBoxLayout()
        top_row.setSpacing(8)
        top_row.addWidget(open_btn)
        top_row.addWidget(pause_btn)
        bottom_row = QHBoxLayout()
        bottom_row.setSpacing(8)
        bottom_row.addWidget(remote_btn)
        bottom_row.addWidget(stop_btn)
        btn_layout.addLayout(top_row)
        btn_layout.addLayout(bottom_row)
        btn_container = QWidget()
        btn_container.setLayout(btn_layout)
        layout.addWidget(btn_container)
        self.setStyleSheet(self.stylesheet())

    def build_tray(self):
        self.tray = QSystemTrayIcon(make_tray_icon(), self)
        self.tray.setToolTip("Sentry is active")

        menu = QMenu()
        open_action = QAction("Open Dashboard", self)
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
            self.system_status_label.setText("● Online" if system_active else "● Offline")
            self.system_status_label.setProperty("state", "active" if system_active else "inactive")
            self.repolish(self.system_status_label)
            
            self.connection_label.setText("Control Center connected")
            self.connection_label.setProperty("state", "ok")
            self.repolish(self.connection_label)

            for key, label in self.status_labels.items():
                state = components.get(key, "disabled")
                training_pct = 0
                if key in summary and "training_percent" in summary[key]:
                    training_pct = int(summary[key]["training_percent"])
                
                if state == "enabled" and training_pct < 100:
                    label.setText("Training")
                    label.setProperty("state", "training")
                elif state == "enabled":
                    label.setText("Running")
                    label.setProperty("state", "on")
                else:
                    label.setText("Inactive")
                    label.setProperty("state", "off")
                self.repolish(label)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            self.connection_label.setText("Waiting for dashboard server")
            self.connection_label.setProperty("state", "bad")
            self.system_status_label.setText("● Offline")
            self.system_status_label.setProperty("state", "inactive")
            self.repolish(self.connection_label)
            self.repolish(self.system_status_label)

    def pause_monitoring(self):
        try:
            request = urllib.request.Request(
                with_token(DASHBOARD_URL.rstrip("/") + "/api/control-all"),
                data=b'{"action": "stop"}',
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(request, timeout=3).read()
        except (urllib.error.URLError, TimeoutError):
            pass

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
            QWidget#root {
                background: #05080d;
                color: #d8e7ee;
                font-family: Segoe UI;
                font-size: 10pt;
                border: 1px solid #1d3340;
                border-radius: 12px;
            }
            QLabel#title {
                color: #64ffda;
                font-size: 18pt;
                font-weight: 700;
            }
            QLabel#connection {
                color: #8ca0ad;
                font-size: 9pt;
            }
            QLabel#connection[state="bad"] {
                color: #ff6b7a;
            }
            QFrame[class="statusBox"] {
                background: #0a1118;
                border: 1px solid #1d3340;
                border-radius: 8px;
            }
            QLabel[state="on"] {
                color: #64ffda;
                font-weight: 700;
            }
            QLabel[state="training"] {
                color: #ffb84d;
                font-weight: 700;
            }
            QLabel[state="off"], QLabel[state="unknown"] {
                color: #ff6b7a;
                font-weight: 700;
            }
            QLabel#systemStatus {
                color: #d8e7ee;
                font-weight: 600;
                font-size: 9pt;
            }
            QLabel#systemStatus[state="active"] {
                color: #64ffda;
                font-weight: 700;
            }
            QLabel#systemStatus[state="inactive"] {
                color: #ff6b7a;
                font-weight: 700;
            }
            QPushButton {
                background: rgba(100, 255, 218, 0.12);
                color: #64ffda;
                border: 1px solid rgba(100, 255, 218, 0.45);
                border-radius: 6px;
                padding: 7px 10px;
            }
            QPushButton:hover {
                background: rgba(100, 255, 218, 0.20);
                border-color: #64ffda;
            }
            QPushButton#danger {
                background: rgba(255, 107, 122, 0.12);
                color: #ff6b7a;
                border-color: rgba(255, 107, 122, 0.50);
            }
            QPushButton#danger:hover {
                background: rgba(255, 107, 122, 0.20);
            }
            QMenu {
                background: #0a1118;
                color: #d8e7ee;
                border: 1px solid #1d3340;
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
