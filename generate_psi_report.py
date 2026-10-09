import os
import sys
import json
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
import smtplib
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

PSI_API_URL = "https://api-open.data.gov.sg/v2/real-time/api/psi"
PM25_API_URL = "https://api-open.data.gov.sg/v2/real-time/api/pm25"
SGT = timezone(timedelta(hours=8))

# --- Credentials from GitHub Secrets ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

SMTP_SERVER = os.environ.get("SMTP_SERVER", "smtp.gmail.com").strip()
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465").strip()) if os.environ.get("SMTP_PORT") else 465
SMTP_USERNAME = os.environ.get("SMTP_USERNAME", "").replace("\xa0", "").strip()
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "").replace("\xa0", "").replace(" ", "").strip()
ALERT_RECIPIENT_RAW = os.environ.get("ALERT_RECIPIENT", "")
ALERT_RECIPIENTS = [
    email.replace("\xa0", "").strip()
    for email in ALERT_RECIPIENT_RAW.split(",")
    if email.strip()
]

REGIONS = ["national", "north", "south", "east", "west", "central"]
REGION_COLORS = {
    "national": "#111827",  # Charcoal / Dark line
    "north": "#0284c7",     # Blue
    "south": "#e11d48",     # Red
    "east": "#10b981",      # Green
    "west": "#f59e0b",      # Orange
    "central": "#8b5cf6",   # Purple
}


def parse_timestamp(ts_str: str) -> datetime:
    clean_ts = ts_str.replace("Z", "+00:00")
    return datetime.fromisoformat(clean_ts).astimezone(SGT)


def fetch_api_records(base_url: str, date_str: str) -> list:
    records = []
    pagination_token = None
    while True:
        params = {"date": date_str}
        if pagination_token:
            params["paginationToken"] = pagination_token
        url = f"{base_url}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"User-Agent": "SG-AirQuality-Reporter/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                if response.status != 200:
                    break
                payload = json.loads(response.read().decode("utf-8"))
        except Exception:
            break

        data_obj = payload.get("data", {})
        batch = data_obj.get("items") or data_obj.get("records") or []
        records.extend(batch)
        pagination_token = data_obj.get("paginationToken")
        if not pagination_token:
            break
    return records


def get_last_24h_data() -> tuple[list, list]:
    now_sgt = datetime.now(SGT)
    yesterday_sgt = now_sgt - timedelta(days=1)
    dates = [yesterday_sgt.strftime("%Y-%m-%d"), now_sgt.strftime("%Y-%m-%d")]
    cutoff_time = now_sgt - timedelta(hours=24)

    # 1. Fetch PSI Records
    raw_psi = []
    for d in dates:
        raw_psi.extend(fetch_api_records(PSI_API_URL, d))

    psi_processed = []
    for item in raw_psi:
        ts_str = item.get("timestamp")
        if not ts_str:
            continue
        ts = parse_timestamp(ts_str)
        if ts >= cutoff_time:
            readings = item.get("readings", {}).get("psi_twenty_four_hourly", {})
            if readings:
                if "national" not in readings:
                    vals = [v for k, v in readings.items() if isinstance(v, (int, float))]
                    readings["national"] = round(sum(vals) / len(vals)) if vals else None
                psi_processed.append({"timestamp": ts, "readings": readings})
    psi_processed.sort(key=lambda x: x["timestamp"])

    # 2. Fetch 1-hr PM2.5 Records
    raw_pm25 = []
    for d in dates:
        raw_pm25.extend(fetch_api_records(PM25_API_URL, d))

    pm25_processed = []
    for item in raw_pm25:
        ts_str = item.get("timestamp")
        if not ts_str:
            continue
        ts = parse_timestamp(ts_str)
        if ts >= cutoff_time:
            raw_r = item.get("readings", {})
            readings = raw_r.get("pm25_one_hourly") or raw_r
            if readings and isinstance(readings, dict):
                clean_readings = {k: v for k, v in readings.items() if isinstance(v, (int, float))}
                if "national" not in clean_readings:
                    vals = list(clean_readings.values())
                    clean_readings["national"] = round(sum(vals) / len(vals)) if vals else None
                pm25_processed.append({"timestamp": ts, "readings": clean_readings})
    pm25_processed.sort(key=lambda x: x["timestamp"])

    return psi_processed, pm25_processed


def get_psi_band(psi: int) -> tuple[str, str]:
    if not isinstance(psi, (int, float)):
        return "Unknown", "#6b7280"
    if psi <= 50:
        return "Good", "#16a34a"
    elif psi <= 100:
        return "Moderate", "#0284c7"
    elif psi <= 200:
        return "Unhealthy", "#d97706"
    elif psi <= 300:
        return "Very Unhealthy", "#ea580c"
    return "Hazardous", "#dc2626"


def get_pm25_band(pm25: int) -> tuple[str, str]:
    if not isinstance(pm25, (int, float)):
        return "Unknown", "#6b7280"
    if pm25 <= 55:
        return "Band I (Normal)", "#16a34a"
    elif pm25 <= 150:
        return "Band II (Elevated)", "#0284c7"
    elif pm25 <= 250:
        return "Band III (High)", "#d97706"
    return "Band IV (Very High)", "#dc2626"


def generate_dual_trend_chart(psi_records: list, pm25_records: list, output_path: str = "air_quality_24h_chart.png"):
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 8), dpi=150, sharex=True)

    # --- Subplot 1: 24-hr PSI ---
    if psi_records:
        psi_ts = [r["timestamp"] for r in psi_records]
        for region in REGIONS:
            vals = [r["readings"].get(region) for r in psi_records]
            if not any(vals):
                continue
            if region == "national":
                ax1.plot(psi_ts, vals, label="Overall (National)", color=REGION_COLORS[region], linewidth=2.5, linestyle="--")
            else:
                ax1.plot(psi_ts, vals, label=region.capitalize(), color=REGION_COLORS[region], linewidth=1.5)

        ax1.axhspan(0, 50, color="#dcfce7", alpha=0.4, label="Good (0-50)")
        ax1.axhspan(50, 100, color="#e0f2fe", alpha=0.4, label="Moderate (51-100)")
        ax1.axhspan(100, 200, color="#fef3c7", alpha=0.4, label="Unhealthy (101-200)")

        max_psi = max([max([v for v in r["readings"].values() if v is not None] or [50]) for r in psi_records] or [60])
        ax1.set_ylim(0, max(max_psi + 15, 65))

    ax1.set_title("Singapore 24-Hour PSI Trend (Overall & Regional)", fontsize=11, fontweight="bold", pad=10)
    ax1.set_ylabel("24-hr PSI Value", fontsize=9, fontweight="semibold")
    ax1.grid(True, linestyle=":", alpha=0.6)
    ax1.legend(loc="upper left", bbox_to_anchor=(1.01, 1), borderaxespad=0, frameon=True, fontsize=8)

    # --- Subplot 2: 1-hr PM2.5 ---
    if pm25_records:
        pm25_ts = [r["timestamp"] for r in pm25_records]
        for region in REGIONS:
            vals = [r["readings"].get(region) for r in pm25_records]
            if not any(vals):
                continue
            if region == "national":
                ax2.plot(pm25_ts, vals, label="Overall (National)", color=REGION_COLORS[region], linewidth=2.5, linestyle="--")
            else:
                ax2.plot(pm25_ts, vals, label=region.capitalize(), color=REGION_COLORS[region], linewidth=1.5)

        ax2.axhspan(0, 55, color="#dcfce7", alpha=0.4, label="Normal (0-55)")
        ax2.axhspan(55, 150, color="#e0f2fe", alpha=0.4, label="Elevated (56-150)")

        max_pm = max([max([v for v in r["readings"].values() if v is not None] or [30]) for r in pm25_records] or [60])
        ax2.set_ylim(0, max(max_pm + 15, 65))

    ax2.set_title("Singapore 1-Hour PM2.5 Trend (Overall & Regional)", fontsize=11, fontweight="bold", pad=10)
    ax2.set_ylabel("1-hr PM2.5 (µg/m³)", fontsize=9, fontweight="semibold")
    ax2.grid(True, linestyle=":", alpha=0.6)
    ax2.legend(loc="upper left", bbox_to_anchor=(1.01, 1), borderaxespad=0, frameon=True, fontsize=8)

    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M\n%d %b", tz=SGT))
    ax2.xaxis.set_major_locator(mdates.HourLocator(interval=3, tz=SGT))

    plt.tight_layout()
    plt.savefig(output_path, bbox_inches="tight")
    plt.close()


def send_telegram(latest_ts: datetime, latest_psi: dict, latest_pm25: dict, chart_path: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    time_str = latest_ts.strftime("%Y-%m-%d %H:%M SGT")
    nat_psi = latest_psi.get("national", "-")
    psi_band, _ = get_psi_band(nat_psi)

    nat_pm = latest_pm25.get("national", "-")
    pm_band, _ = get_pm25_band(nat_pm)

    lines = []
    for r in REGIONS:
        r_label = "Overall (National)" if r == "national" else r.capitalize()
        p_val = latest_psi.get(r, "-")
        pm_val = latest_pm25.get(r, "-")
        lines.append(f"• *{r_label}:* PSI `{p_val}` | PM2.5 `{pm_val}` µg/m³")

    regional_text = "\n".join(lines)
    caption = (
        f"🇸🇬 *Singapore Air Quality Hourly Report*\n"
        f"🕒 Updated: {time_str}\n\n"
        f"• *24-hr PSI Status:* {nat_psi} ({psi_band})\n"
        f"• *1-hr PM2.5 Status:* {nat_pm} µg/m³ ({pm_band})\n\n"
        f"{regional_text}\n\n"
        f"📈 _24-Hour PSI & PM2.5 Dual-Trend Chart attached below._"
    )

    url_photo = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    boundary = "----WebKitFormBoundarySGReport"
    body = bytearray()
    for name, value in [("chat_id", TELEGRAM_CHAT_ID), ("caption", caption), ("parse_mode", "Markdown")]:
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
        body.extend(f"{value}\r\n".encode("utf-8"))

    body.extend(f"--{boundary}\r\n".encode("utf-8"))
    body.extend(b'Content-Disposition: form-data; name="photo"; filename="chart.png"\r\nContent-Type: image/png\r\n\r\n')
    with open(chart_path, "rb") as f:
        body.extend(f.read())
    body.extend(f"\r\n--{boundary}--\r\n".encode("utf-8"))

    req = urllib.request.Request(url_photo, data=bytes(body), headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=20):
            print("Telegram photo report sent.")
    except Exception as e:
        print(f"Failed to send Telegram report: {e}")


def send_email(latest_ts: datetime, latest_psi: dict, latest_pm25: dict, chart_path: str):
    if not SMTP_USERNAME or not SMTP_PASSWORD or not ALERT_RECIPIENTS:
        return

    nat_psi = latest_psi.get("national", "N/A")
    psi_band, psi_color = get_psi_band(nat_psi)
    nat_pm = latest_pm25.get("national", "N/A")
    pm_band, pm_color = get_pm25_band(nat_pm)
    formatted_time = latest_ts.strftime("%Y-%m-%d %H:%M SGT")

    subject = f"[PSI: {nat_psi} | PM2.5: {nat_pm} µg/m³] Singapore Hourly Air Quality Report - {latest_ts.strftime('%H:%M SGT')}"

    rows_html = ""
    for r in REGIONS:
        p_val = latest_psi.get(r, "-")
        p_band, p_col = get_psi_band(p_val)
        pm_val = latest_pm25.get(r, "-")
        pm_b, pm_col = get_pm25_band(pm_val)
        label = "Overall (National)" if r == "national" else r.capitalize()

        rows_html += f"""
        <tr style="border-bottom: 1px solid #e5e7eb;">
            <td style="padding: 9px 12px; font-weight: {'bold' if r == 'national' else 'normal'};">{label}</td>
            <td style="padding: 9px 12px; font-weight: bold; color: {p_col};">{p_val}</td>
            <td style="padding: 9px 12px;">
                <span style="background-color: {p_col}; color: #ffffff; padding: 2px 7px; border-radius: 4px; font-size: 11px;">{p_band}</span>
            </td>
            <td style="padding: 9px 12px; font-weight: bold; color: {pm_col};">{pm_val} µg/m³</td>
            <td style="padding: 9px 12px;">
                <span style="background-color: {pm_col}; color: #ffffff; padding: 2px 7px; border-radius: 4px; font-size: 11px;">{pm_b}</span>
            </td>
        </tr>
        """

    html_content = f"""
    <!DOCTYPE html>
    <html>
    <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #f3f4f6; padding: 20px; color: #1f2937;">
        <div style="max-width: 720px; margin: 0 auto; background: #ffffff; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.1);">
            <div style="background-color: {psi_color}; color: #ffffff; padding: 16px 20px;">
                <h2 style="margin: 0; font-size: 19px;">Singapore Hourly Air Quality Report</h2>
                <p style="margin: 4px 0 0 0; font-size: 13px; opacity: 0.95;">Updated: {formatted_time}</p>
            </div>
            <div style="padding: 20px;">
                <h3 style="margin-top: 0; font-size: 15px; color: #374151;">Latest Regional Air Quality Summary</h3>
                <table style="width: 100%; border-collapse: collapse; text-align: left; font-size: 13px; margin-bottom: 24px;">
                    <thead>
                        <tr style="background-color: #f9fafb; border-bottom: 2px solid #e5e7eb;">
                            <th style="padding: 8px 12px;">Region</th>
                            <th style="padding: 8px 12px;">24-hr PSI</th>
                            <th style="padding: 8px 12px;">PSI Status</th>
                            <th style="padding: 8px 12px;">1-hr PM2.5</th>
                            <th style="padding: 8px 12px;">PM2.5 Band</th>
                        </tr>
                    </thead>
                    <tbody>{rows_html}</tbody>
                </table>

                <h3 style="font-size: 15px; color: #374151; margin-bottom: 10px;">24-Hour Trend Charts (PSI & PM2.5)</h3>
                <div style="text-align: center;">
                    <img src="cid:air_chart" alt="24-Hour Air Quality Trend Chart" style="max-width: 100%; height: auto; border-radius: 6px; border: 1px solid #e5e7eb;" />
                </div>

                <p style="font-size: 12px; color: #9ca3af; margin-top: 24px; text-align: center;">
                    Data retrieved via <a href="https://data.gov.sg/datasets/d_fe37906a0182569d891506e815e819b7/view" style="color: #2563eb;">data.gov.sg PSI API</a> and <a href="https://data.gov.sg/datasets/d_e1058d6974c877257e32048ab128ad83/view" style="color: #2563eb;">PM2.5 API</a>.
                </p>
            </div>
        </div>
    </body>
    </html>
    """

    msg = MIMEMultipart("related")
    msg["Subject"] = subject
    msg["From"] = SMTP_USERNAME
    msg["To"] = ", ".join(ALERT_RECIPIENTS)

    msg_alt = MIMEMultipart("alternative")
    msg.attach(msg_alt)
    msg_alt.attach(MIMEText(html_content, "html"))

    with open(chart_path, "rb") as img_file:
        img = MIMEImage(img_file.read())
        img.add_header("Content-ID", "<air_chart>")
        img.add_header("Content-Disposition", "inline", filename="air_quality_chart.png")
        msg.attach(img)

    try:
        if SMTP_PORT == 465:
            with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as server:
                server.login(SMTP_USERNAME, SMTP_PASSWORD)
                server.sendmail(SMTP_USERNAME, ALERT_RECIPIENTS, msg.as_string())
        else:
            with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
                server.starttls()
                server.login(SMTP_USERNAME, SMTP_PASSWORD)
                server.sendmail(SMTP_USERNAME, ALERT_RECIPIENTS, msg.as_string())
        print(f"Email report sent to: {', '.join(ALERT_RECIPIENTS)}")
    except Exception as e:
        print(f"Failed to send email: {e}")


def main():
    print("Fetching PSI & 1-hour PM2.5 data from data.gov.sg...")
    psi_records, pm25_records = get_last_24h_data()
    if not psi_records and not pm25_records:
        print("No records retrieved.")
        return

    latest_psi_item = psi_records[-1] if psi_records else {"timestamp": datetime.now(SGT), "readings": {}}
    latest_pm25_item = pm25_records[-1] if pm25_records else {"timestamp": datetime.now(SGT), "readings": {}}

    latest_ts = latest_psi_item["timestamp"]
    chart_file = "air_quality_24h_chart.png"
    generate_dual_trend_chart(psi_records, pm25_records, chart_file)

    send_telegram(latest_ts, latest_psi_item["readings"], latest_pm25_item["readings"], chart_file)
    send_email(latest_ts, latest_psi_item["readings"], latest_pm25_item["readings"], chart_file)


if __name__ == "__main__":
    main()
