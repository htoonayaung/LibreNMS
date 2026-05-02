import os
import requests
import pandas as pd
from datetime import datetime

try:
    from dotenv import load_dotenv, find_dotenv
except ImportError:
    load_dotenv = None

script_dir = os.path.dirname(os.path.abspath(__file__))
if load_dotenv is not None:
    dotenv_path = find_dotenv(usecwd=True)
    if dotenv_path:
        load_dotenv(dotenv_path)

# --- Configuration ---
LIBRENMS_TOKEN = os.getenv('LIBRENMS_TOKEN') or os.getenv('LIBRENMS_API_TOKEN')
LIBRENMS_URL = os.getenv('LIBRENMS_URL', 'http://10.0.34.55/api/v0').rstrip('/')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN') or os.getenv('TELEGRAM_BOT_TOKEN')
CHAT_ID = os.getenv('CHAT_ID') or os.getenv('TELEGRAM_CHAT_ID')
HEADERS = {'X-Auth-Token': LIBRENMS_TOKEN} if LIBRENMS_TOKEN else {}
REPORT_SESSION = requests.Session()
if HEADERS:
    REPORT_SESSION.headers.update(HEADERS)

def fetch_api(endpoint):
    try:
        r = REPORT_SESSION.get(f"{LIBRENMS_URL}/{endpoint}", timeout=30)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.HTTPError as e:
        print(f"HTTP Error fetching {endpoint}: {e}")
        return {}
    except requests.exceptions.ConnectionError as e:
        print(f"Connection Error fetching {endpoint}: {e}")
        return {}
    except requests.exceptions.Timeout as e:
        print(f"Timeout Error fetching {endpoint}: {e}")
        return {}
    except requests.exceptions.RequestException as e:
        print(f"Generic Request Error fetching {endpoint}: {e}")
        return {}

def get_device_data(devices_data):
    print("🚀 Extracting Comprehensive ISP Network Data...")
    # devices_data = fetch_api("devices").get("devices", []) # devices_data is now passed as an argument
    processors = fetch_api("processors").get("processors", [])
    mempools = fetch_api("mempools").get("mempools", [])
    temp_sensors = fetch_api("sensors?type=temperature").get("sensors", [])
    ports_data = fetch_api("ports?sort=ifInOctets_rate%20desc").get("ports", [])[:50]

    # Fetch availability and outages for each device
    device_availability = {}
    device_outages = {}
    for device in devices_data:
        hostname = device.get("hostname")
        if hostname:
            availability = fetch_api(f"devices/{hostname}/availability").get("availability", [])
            outages = fetch_api(f"devices/{hostname}/outages").get("outages", [])
            device_availability[hostname] = availability
            device_outages[hostname] = outages

    return devices_data, processors, mempools, temp_sensors, ports_data, device_availability, device_outages

def generate_excel_report(devices_data, processors, mempools, temp_sensors, ports_data, device_availability, device_outages):
    filename = f"ISP_Health_Report_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
    
    with pd.ExcelWriter(filename, engine='openpyxl') as writer:
        # Sheet 1: Inventory & Uptime
        if devices_data:
            df_dev = pd.DataFrame(devices_data)
            cols = ["hostname", "ip", "status", "uptime", "hardware", "os", "last_polled"]
            valid_cols = [c for c in cols if c in df_dev.columns]

            # Add availability and outages to the device data
            df_dev["availability_24h_perc"] = df_dev["hostname"].apply(lambda x: next((item["availability_perc"] for item in device_availability.get(x, []) if item["duration"] == 86400), "N/A"))
            df_dev["outages_count"] = df_dev["hostname"].apply(lambda x: len(device_outages.get(x, [])))

            df_dev[valid_cols + ["availability_24h_perc", "outages_count"]].to_excel(writer, sheet_name="Device_Inventory", index=False)

        # Sheet 2: Performance (CPU, RAM, Temp)
        perf_summary = []
        cpu_map = {p['hostname']: p.get('processor_usage') for p in processors if 'hostname' in p}
        mem_map = {m['hostname']: m.get('mempool_perc') for m in mempools if 'hostname' in m}
        temp_map = {s['hostname']: s.get('sensor_current') for s in temp_sensors if 'hostname' in s}

        for d in devices_data:
            h = d.get('hostname')
            perf_summary.append({
                'Hostname': h,
                'IP Address': d.get('ip'),
                'CPU Usage (%)': cpu_map.get(h, "N/A"),
                'RAM Usage (%)': mem_map.get(h, "N/A"),
                'Temp (°C)': temp_map.get(h, "N/A"),
                'Hardware': d.get('hardware')
            })
        if perf_summary:
            pd.DataFrame(perf_summary).to_excel(writer, sheet_name='Performance_Report', index=False)

        # Sheet 3: Top Traffic Usage
        traffic_list = []
        for p in ports_data:
            in_mbps = round((float(p.get('ifInOctets_rate', 0)) * 8) / 1000000, 2)
            out_mbps = round((float(p.get('ifOutOctets_rate', 0)) * 8) / 1000000, 2)
            traffic_list.append({
                'Device': p.get('hostname'),
                'Port': p.get('ifName'),
                'Description': p.get('ifAlias'),
                'In (Mbps)': in_mbps,
                'Out (Mbps)': out_mbps,
                'Port Status': p.get('ifOperStatus')
            })
        if traffic_list:
            pd.DataFrame(traffic_list).to_excel(writer, sheet_name='Traffic_Monitoring', index=False)
    return filename

def send_telegram_report(filename):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print('Telegram credentials are not configured. Skipping report delivery.')
        return False

    print(f"📤 Uploading {filename} to Telegram...")
    tg_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument"
    try:
        with open(filename, 'rb') as f:
            caption = (f"📊 *ISP Network Report*\n"
                       f"🗓 Date: {datetime.now().strftime('%d-%m-%Y')}\n"
                       f"⏰ Time: {datetime.now().strftime('%H:%M')}\n"
                       f"✅ Status: All Systems Audited")
            r = requests.post(tg_url, data={'chat_id': CHAT_ID, 'caption': caption, 'parse_mode': 'Markdown'}, files={'document': f})
            if r.status_code == 200:
                print("✅ Report Sent Successfully!")
                return True
            print(f"❌ Telegram upload failed: {r.status_code} {r.text}")
            return False
    except FileNotFoundError:
        print(f"❌ Report file not found: {filename}")
        return False
    except Exception as e:
        print(f"Final Error: {e}")
        return False


def main():
    if not LIBRENMS_TOKEN:
        print('LIBRENMS_TOKEN or LIBRENMS_API_TOKEN is required to fetch LibreNMS data.')
        return

    initial_devices_data = fetch_api("devices").get("devices", [])
    devices_data, processors, mempools, temp_sensors, ports_data, device_availability, device_outages = get_device_data(initial_devices_data)
    report_filename = generate_excel_report(devices_data, processors, mempools, temp_sensors, ports_data, device_availability, device_outages)
    send_telegram_report(report_filename)


if __name__ == "__main__":
    main()
