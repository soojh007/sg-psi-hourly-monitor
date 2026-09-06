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

API_BASE_URL = "https://api-open.data.gov.sg/v2/real-time/api/psi"
SGT = timezone(timedelta(hours=8))

# Configuration from Environment Variables
SMTP_SERVER = os.environ.get("SMTP_SERVER", "smtp.gmail.com").strip()
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465").strip())
SMTP_USERNAME = os.environ.get("SMTP_USERNAME", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
ALERT_RECIPIENT_RAW = os.environ.get("ALERT_RECIPIENT", "")

# Auto-sanitize credentials: remove invisible non-breaking spaces (\xa0) and normal spaces
if SMTP_USERNAME:
    SMTP_USERNAME = SMTP_USERNAME.replace("\xa0", "").strip()
if SMTP_PASSWORD:
    # Google App Passwords are 16 characters with no spaces
    SMTP_PASSWORD = SMTP_PASSWORD.replace("\xa0", "").replace(" ", "").strip()

# Split multiple comma-separated recipients and strip whitespace/\xa0
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
    dt = datetime.fromisoformat(clean_ts)
    return dt.astimezone(SGT)


def fetch_date_records(date_str: str) -> list:
    records = []
    pagination_token = None

    while True:
        params = {"date": date_str}
        if pagination_token:
            params["paginationToken"] = pagination_token

        url = f"{API_BASE_URL}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"User-Agent": "SG-PSI-Reporter/1.0"})

        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                if response.status != 200:
                    break
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as e:
            print(f"Error fetching data for {date_str}: {e}")
            break

        data_obj = payload.get("data", {})
        batch = data_obj.get("items") or data_obj.get("records") or []
        records.extend(batch)

        pagination_token = data_obj.get("paginationToken")
        if not pagination_token:
            break

    return records


def get_last_24h_data() -> list:
    now_sgt = datetime.now(SGT)
    yesterday_sgt = now_sgt - timedelta(days=1)

    raw_records = []
    raw_records.extend(fetch_date_records(yesterday_sgt.strftime("%Y-%m-%d")))
    raw_records.extend(fetch_date_records(now_sgt.strftime("%Y-%m-%d")))

    cutoff_time = now_sgt - timedelta(hours=24)
    processed = []

    for item in raw_records:
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
                processed.append({"timestamp": ts, "readings": readings})

    processed.sort(key=lambda x: x["timestamp"])
    return processed


def generate_chart(records: list, output_path: str = "psi_24h_chart.png"):
    timestamps = [r["timestamp"] for r in records]

    fig, ax = plt.subplots(figsize=(10, 5), dpi=150)

    for region in REGIONS:
        y_values = [r["readings"].get(region) for r in records]
        if not any(y_values):
            continue

        if region == "national":
            ax.plot(timestamps, y_values, label="Overall (National)", color=REGION_COLORS[region], linewidth=2.5, linestyle="--")
        else:
            ax.plot(timestamps, y_values, label=region.capitalize(), color=REGION_COLORS[region], linewidth=1.5)

    ax.axhspan(0, 50, color="#dcfce7", alpha=0.4, label="Good (0-50)")
    ax.axhspan(50, 100, color="#e0f2fe", alpha=0.4, label="Moderate (51-100)")
    ax.axhspan(100, 200, color="#fef3c7", alpha=0.4, label="Unhealthy (101-200)")

    max_psi = max([max([v for v in r["readings"].values() if v is not None] or [50]) for r in records] or [60])
    ax.set_ylim(0, max(max_psi + 15, 65))
    ax.set_title("Singapore 24-Hour PSI Trend (Overall & Regional)", fontsize=13, fontweight="bold", pad=12)
    ax.set_ylabel("24-hr PSI Value", fontsize=10)
    ax.grid(True, linestyle=":", alpha=0.6)

    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M\n%d %b", tz=SGT))
    ax.xaxis.set_major_locator(mdates.HourLocator(interval=3, tz=SGT))

    ax.legend(loc="upper left", bbox_to_anchor=(1.01, 1), borderaxespad=0, frameon=True, fontsize=9)
    plt.tight_layout()
    plt.savefig(output_path, bbox_inches="tight")
    plt.close()


def get_band_details(psi: int) -> tuple[str, str]:
    if psi <= 50:
        return "Good", "#16a34a"
    elif psi <= 100:
        return "Moderate", "#0284c7"
    elif psi <= 200:
        return "Unhealthy", "#d97706"
    elif psi <= 300:
        return "Very Unhealthy", "#ea580c"
    return "Hazardous", "#dc2626"


def send_email_report(latest_ts: datetime, latest_readings: dict, chart_path: str):
    if not all([SMTP_USERNAME, SMTP_PASSWORD, ALERT_RECIPIENT]):
        print("Missing SMTP credentials. Exiting.")
        sys.exit(1)

    national_val = latest_readings.get("national", "N/A")
    band_name, band_color = get_band_details(national_val) if isinstance(national_val, int) else ("Unknown", "#6b7280")
    formatted_time = latest_ts.strftime("%Y-%m-%d %H:%M SGT")

    subject = f"[PSI: {national_val} ({band_name})] Singapore Hourly PSI Report - {latest_ts.strftime('%H:%M SGT')}"

    rows_html = ""
    for r in REGIONS:
        val = latest_readings.get(r, "-")
        r_band, r_color = get_band_details(val) if isinstance(val, int) else ("-", "#6b7280")
        label = "Overall (National)" if r == "national" else r.capitalize()
        rows_html += f"""
        <tr style="border-bottom: 1px solid #e5e7eb;">
            <td style="padding: 9px 12px; font-weight: {'bold' if r == 'national' else 'normal'};">{label}</td>
            <td style="padding: 9px 12px; font-weight: bold; color: {r_color}; font-size: 15px;">{val}</td>
            <td style="padding: 9px 12px;">
                <span style="background-color: {r_color}; color: #ffffff; padding: 2px 8px; border-radius: 4px; font-size: 12px;">
                    {r_band}
                </span>
            </td>
        </tr>
        """

    html_content = f"""
    <!DOCTYPE html>
    <html>
    <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #f3f4f6; padding: 20px; color: #1f2937;">
        <div style="max-width: 680px; margin: 0 auto; background: #ffffff; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.1);">
            <div style="background-color: {band_color}; color: #ffffff; padding: 16px 20px;">
                <h2 style="margin: 0; font-size: 19px;">Singapore Hourly PSI Report</h2>
                <p style="margin: 4px 0 0 0; font-size: 13px; opacity: 0.95;">Updated: {formatted_time}</p>
            </div>
            <div style="padding: 20px;">
                <h3 style="margin-top: 0; font-size: 15px; color: #374151;">Latest Regional 24-hr PSI Readings</h3>
                <table style="width: 100%; border-collapse: collapse; text-align: left; font-size: 14px; margin-bottom: 24px;">
                    <thead>
                        <tr style="background-color: #f9fafb; border-bottom: 2px solid #e5e7eb;">
                            <th style="padding: 8px 12px;">Region</th>
                            <th style="padding: 8px 12px;">PSI</th>
                            <th style="padding: 8px 12px;">Status</th>
                        </tr>
                    </thead>
                    <tbody>{rows_html}</tbody>
                </table>
                <h3 style="font-size: 15px; color: #374151; margin-bottom: 10px;">24-Hour Trend Chart</h3>
                <div style="text-align: center;">
                    <img src="cid:psi_chart" alt="24-Hour PSI Trend Chart" style="max-width: 100%; height: auto; border-radius: 6px; border: 1px solid #e5e7eb;" />
                </div>
                <p style="font-size: 12px; color: #9ca3af; margin-top: 24px; text-align: center;">
                    Source: data.gov.sg
                </p>
            </div>
        </div>
    </body>
    </html>
    """

    msg = MIMEMultipart("related")
    msg["Subject"] = subject
    msg["From"] = SMTP_USERNAME
    msg["To"] = ALERT_RECIPIENT

    msg_alt = MIMEMultipart("alternative")
    msg.attach(msg_alt)
    msg_alt.attach(MIMEText(html_content, "html"))

    with open(chart_path, "rb") as img_file:
        img = MIMEImage(img_file.read())
        img.add_header("Content-ID", "<psi_chart>")
        img.add_header("Content-Disposition", "inline", filename=os.path.basename(chart_path))
        msg.attach(img)

    if SMTP_PORT == 465:
        with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as server:
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.sendmail(SMTP_USERNAME, ALERT_RECIPIENT, msg.as_string())
    else:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.sendmail(SMTP_USERNAME, ALERT_RECIPIENT, msg.as_string())

    print(f"Hourly report sent to {ALERT_RECIPIENT}.")


def main():
    print("Fetching last 24 hours of PSI data from data.gov.sg...")
    records = get_last_24h_data()
    if not records:
        print("No records retrieved within the last 24 hours.")
        return

    latest_item = records[-1]
    chart_file = "psi_24h_chart.png"
    generate_chart(records, chart_file)
    send_email_report(latest_item["timestamp"], latest_item["readings"], chart_file)


if __name__ == "__main__":
    main()
