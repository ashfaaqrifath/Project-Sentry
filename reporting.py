from io import BytesIO
from datetime import datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages

BASE_DIR = Path(__file__).resolve().parent
A4_WIDTH = 8.27
A4_HEIGHT = 11.69

COLORS = {
    "ink": "#17242b",
    "muted": "#63747b",
    "mint": "#147d70",
    "red": "#b8424d",
    "amber": "#b47a13",
    "pale": "#e6f5f1",
}


def _number(value, default=0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _module_rows(summary):
    return [
        ("Keystroke dynamics", summary.get("keystroke", {}), "anomalies_24h"),
        ("Mouse dynamics", summary.get("mouse", {}), "anomalies_24h"),
        ("Network usage", summary.get("network", {}), "anomalies_24h"),
        ("Drive health", summary.get("drive", {}), "alerts"),
    ]


def _draw_header(axis, summary):
    overall = summary.get("overall", {})
    risk = str(overall.get("risk", "unknown")).upper()
    risk_score = _number(overall.get("risk_score"))
    username = summary.get("username", "unknown")
    generated = summary.get("generated_at") or datetime.now().strftime("%Y-%m-%d %H:%M")

    axis.axis("off")
    axis.text(0.02, 0.86, "SENTRY ANOMALY REPORT", fontsize=20, weight="bold", color=COLORS["ink"])
    axis.text(0.02, 0.65, f"Generated {generated}  |  User {username}", fontsize=9, color=COLORS["muted"])
    axis.text(0.02, 0.42, f"Overall status: {risk}    Risk score: {risk_score:.0f}/100", fontsize=11, weight="bold", color=COLORS["red"] if risk == "ANOMALY" else COLORS["mint"])


def _draw_summary(axis, summary):
    axis.axis("off")
    modules = _module_rows(summary)
    anomaly_total = sum(_number(module.get(key)) for _, module, key in modules)
    alerts = summary.get("alerts", []) or []
    status = summary.get("user_status", {})
    overall = summary.get("overall", {})
    cards = [
        ("ANOMALIES / 24H", f"{anomaly_total:.0f}"),
        ("RISK SCORE", f"{_number(overall.get('risk_score')):.0f}/100"),
        ("RECENT EVENTS", str(len(alerts))),
        ("SYSTEM UPTIME", str(status.get("uptime", "unknown"))),
    ]
    for index, (label, value) in enumerate(cards):
        x = 0.02 + index * 0.245
        axis.add_patch(plt.Rectangle((x, 0.18), 0.22, 0.62, facecolor=COLORS["pale"], edgecolor="none"))
        axis.text(x + 0.02, 0.65, label, fontsize=8, color=COLORS["muted"])
        axis.text(x + 0.02, 0.37, value, fontsize=15, weight="bold", color=COLORS["ink"])


def _draw_distribution(axis, summary):
    names = []
    values = []
    for name, module, key in _module_rows(summary):
        names.append(name)
        values.append(_number(module.get("risk_score")) if key == "alerts" else _number(module.get("anomaly_percent")))

    axis.barh(names[::-1], values[::-1], color=COLORS["mint"])
    axis.set_xlim(0, 100)
    axis.set_xlabel("Risk / anomaly rate (%)", color=COLORS["muted"])
    axis.tick_params(colors=COLORS["muted"], labelsize=8)
    axis.set_title("Anomaly distribution by detector", loc="left", color=COLORS["ink"], weight="bold")
    for spine in axis.spines.values():
        spine.set_visible(False)
    axis.grid(axis="x", alpha=0.16)


def _draw_trends(axis, summary):
    insights = summary.get("behavioral_insights", {}) or {}
    configs = [
        ("Keystroke", "typing_speed_trend"),
        ("Mouse", "mouse_dynamics_trend"),
        ("Network", "network_usage_trend"),
        ("Drive", "drive_health_trend"),
    ]
    for index, (title, key) in enumerate(configs):
        chart = axis[index]
        points = insights.get(key, []) or []
        values = [_number(point.get("speed")) for point in points]
        labels = [str(point.get("date", "")) for point in points]
        chart.bar(range(len(values)), values, color=COLORS["mint"], width=0.72)
        chart.set_title(title, loc="left", fontsize=9, weight="bold", color=COLORS["ink"])
        chart.set_xticks(range(len(labels)))
        chart.set_xticklabels(labels, fontsize=6, color=COLORS["muted"])
        chart.tick_params(axis="y", labelsize=7, colors=COLORS["muted"])
        chart.grid(axis="y", alpha=0.16)
        for spine in chart.spines.values():
            spine.set_visible(False)


def _draw_alerts(axis, summary):
    axis.axis("off")
    axis.text(0.02, 0.96, "RECENT ANOMALY EVENTS", fontsize=13, weight="bold", color=COLORS["ink"])
    alerts = summary.get("alerts", []) or []
    if not alerts:
        axis.text(0.02, 0.86, "No anomaly events were recorded in the current snapshot.", fontsize=10, color=COLORS["muted"])
        return

    y = 0.87
    for alert in alerts[:18]:
        source = str(alert.get("source", "unknown")).upper()
        timestamp = str(alert.get("timestamp", "unknown"))
        message = str(alert.get("summary", "Anomaly detected"))
        axis.text(0.02, y, source, fontsize=8, weight="bold", color=COLORS["red"])
        axis.text(0.19, y, timestamp, fontsize=8, color=COLORS["muted"])
        axis.text(0.43, y, message[:90], fontsize=8, color=COLORS["ink"])
        y -= 0.045


def _draw_footer(axis, page_number):
    axis.axis("off")
    axis.text(
        0.02,
        0.45,
        "Project-Sentry · Backend PDF report · A4 portrait",
        fontsize=8,
        color=COLORS["muted"],
        transform=axis.transAxes,
    )
    axis.text(
        0.98,
        0.45,
        f"Page {page_number}",
        fontsize=8,
        color=COLORS["muted"],
        ha="right",
        transform=axis.transAxes,
    )


def build_anomaly_report_pdf(summary):
    output = BytesIO()
    with PdfPages(output) as pdf:
        first = plt.figure(figsize=(A4_WIDTH, A4_HEIGHT), facecolor="white")
        header = first.add_axes((0.06, 0.78, 0.88, 0.17))
        _draw_header(header, summary)
        cards = first.add_axes((0.06, 0.57, 0.88, 0.15))
        _draw_summary(cards, summary)
        distribution = first.add_axes((0.08, 0.33, 0.38, 0.22))
        _draw_distribution(distribution, summary)
        trend_axes = [
            first.add_axes((0.56, 0.33, 0.16, 0.18)),
            first.add_axes((0.74, 0.33, 0.16, 0.18)),
            first.add_axes((0.56, 0.10, 0.16, 0.18)),
            first.add_axes((0.74, 0.10, 0.16, 0.18)),
        ]
        _draw_trends(trend_axes, summary)
        footer = first.add_axes((0.06, 0.02, 0.88, 0.05))
        _draw_footer(footer, 1)
        pdf.savefig(first, bbox_inches="tight")
        plt.close(first)

        second = plt.figure(figsize=(A4_WIDTH, A4_HEIGHT), facecolor="white")
        header = second.add_axes((0.06, 0.78, 0.88, 0.17))
        _draw_header(header, summary)
        alerts = second.add_axes((0.06, 0.18, 0.88, 0.56))
        _draw_alerts(alerts, summary)
        footer = second.add_axes((0.06, 0.02, 0.88, 0.05))
        _draw_footer(footer, 2)
        pdf.savefig(second, bbox_inches="tight")
        plt.close(second)

    return output.getvalue()
