# refactor-ready Flask app
from flask import Flask, render_template, jsonify, url_for, redirect, request, session, Response
import requests
from concurrent.futures import ThreadPoolExecutor
import json
import os
import logging
import sqlite3
import threading
import time
import sys
import functools
import csv
from datetime import datetime, timezone, timedelta
from io import StringIO
import pymysql
from werkzeug.security import generate_password_hash, check_password_hash

try:
    from dotenv import load_dotenv, find_dotenv
except ImportError:
    load_dotenv = None
    find_dotenv = None

logging.basicConfig(level=logging.INFO)
script_dir = os.path.dirname(os.path.abspath(__file__))
if load_dotenv is not None:
    env_path = find_dotenv(usecwd=True) if find_dotenv is not None else os.path.join(script_dir, '.env')
    if env_path and os.path.exists(env_path):
        load_dotenv(env_path)
    else:
        logging.warning('No .env file found; using environment variables only.')
else:
    logging.warning('python-dotenv not installed; .env loading skipped.')

def load_secret_key():
    env_key = os.getenv('SECRET_KEY')
    if env_key:
        return env_key

    secret_file = os.getenv('SECRET_KEY_FILE', os.path.join(script_dir, '.secret_key'))
    try:
        if os.path.exists(secret_file):
            with open(secret_file, 'rb') as f:
                return f.read().decode('utf-8')
    except Exception as exc:
        logging.warning('Failed to read secret key file %s: %s', secret_file, exc)

    secret_key = os.urandom(32).hex()
    try:
        with open(secret_file, 'w', encoding='utf-8') as f:
            f.write(secret_key)
        os.chmod(secret_file, 0o600)
        logging.info('Generated persistent secret key file at %s', secret_file)
    except Exception as exc:
        logging.warning('Failed to write secret key file %s: %s', secret_file, exc)
    return secret_key

app = Flask(__name__)
app.secret_key = load_secret_key()
session_cookie_secure = os.getenv('SESSION_COOKIE_SECURE', 'false').lower() in ('1', 'true', 'yes')
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = session_cookie_secure
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=7)

@app.after_request
def set_security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    response.headers['Permissions-Policy'] = 'geolocation=()'
    if session_cookie_secure or request.is_secure:
        response.headers['Strict-Transport-Security'] = 'max-age=63072000; includeSubDomains; preload'
    return response

# --- CONFIGURATION ---
LIBRENMS_URL = os.getenv('LIBRENMS_URL', 'http://10.0.34.55/api/v0').rstrip('/')
API_TOKEN = os.getenv('LIBRENMS_API_TOKEN')
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
DB_HOST = os.getenv('DB_HOST', '127.0.0.1')
DB_USER = os.getenv('DB_USER', 'librenms')
DB_PASSWORD = os.getenv('DB_PASSWORD', 'LibreNMS@123')
DB_NAME = os.getenv('DB_NAME', 'librenms')
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}" if TELEGRAM_BOT_TOKEN else None
TELEGRAM_WEBHOOK_URL = os.getenv('TELEGRAM_WEBHOOK_URL')
TELEGRAM_SESSION = requests.Session()
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
OPENAI_MODEL = os.getenv('OPENAI_MODEL', 'gpt-3.5-turbo')
ADMIN_USERNAME = os.getenv('ADMIN_USERNAME')
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD')
APP_DB_PATH = os.getenv('APP_DB_PATH', os.path.join(script_dir, 'app.db'))
OPENAI_API_URL = 'https://api.openai.com/v1/chat/completions'
MAX_LOGIN_ATTEMPTS = int(os.getenv('MAX_LOGIN_ATTEMPTS', '5'))
LOGIN_LOCKOUT_SECONDS = int(os.getenv('LOGIN_LOCKOUT_SECONDS', '300'))
DASHBOARD_CACHE_TTL = int(os.getenv('DASHBOARD_CACHE_TTL', '15'))
# ---------------------

HEADERS = {'X-Auth-Token': API_TOKEN} if API_TOKEN else {}
if not API_TOKEN:
    logging.warning('LIBRENMS_API_TOKEN is not set. API calls will fail until it is configured.')

API_TIMEOUT = (5, 15)
LIBRENMS_SESSION = requests.Session()
if HEADERS:
    LIBRENMS_SESSION.headers.update(HEADERS)

config_path = os.path.join(script_dir, 'config.json')
try:
    with open(config_path, 'r') as f:
        ALERT_THRESHOLDS = json.load(f)
except FileNotFoundError:
    ALERT_THRESHOLDS = {
        'cpu_high': 85,
        'cpu_warning': 70,
        'mem_high': 90,
        'mem_warning': 80,
        'temp_high': 80,
        'temp_warning': 70
    }


def format_uptime(seconds):
    try:
        seconds = int(seconds)
        days, remainder = divmod(seconds, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, seconds = divmod(remainder, 60)
        if days > 0:
            return f"{days}d {hours}h {minutes}m"
        elif hours > 0:
            return f"{hours}h {minutes}m"
        else:
            return f"{minutes}m {seconds}s"
    except Exception:
        return 'N/A'


def get_cpu(device_id):
    try:
        conn = pymysql.connect(
            host=DB_HOST,
            user=DB_USER,
            password=DB_PASSWORD,
            db=DB_NAME,
            cursorclass=pymysql.cursors.DictCursor,
        )
        with conn.cursor() as cursor:
            cursor.execute("SELECT AVG(processor_usage) as avg_cpu FROM processors WHERE device_id = %s", (device_id,))
            result = cursor.fetchone()
            return round(result['avg_cpu']) if result and result['avg_cpu'] is not None else 0
    except Exception as exc:
        logging.error('get_cpu query failed: %s', exc)
        return 0
    finally:
        try:
            conn.close()
        except Exception:
            pass


def get_mem(device_id):
    try:
        conn = pymysql.connect(
            host=DB_HOST,
            user=DB_USER,
            password=DB_PASSWORD,
            db=DB_NAME,
            cursorclass=pymysql.cursors.DictCursor,
        )
        with conn.cursor() as cursor:
            cursor.execute("SELECT AVG(mempool_perc) as avg_mem FROM mempools WHERE device_id = %s", (device_id,))
            result = cursor.fetchone()
            return round(result['avg_mem']) if result and result['avg_mem'] is not None else 0
    except Exception as exc:
        logging.error('get_mem query failed: %s', exc)
        return 0
    finally:
        try:
            conn.close()
        except Exception:
            pass


def get_temp(device_id):
    try:
        conn = pymysql.connect(
            host=DB_HOST,
            user=DB_USER,
            password=DB_PASSWORD,
            db=DB_NAME,
            cursorclass=pymysql.cursors.DictCursor,
        )
        with conn.cursor() as cursor:
            cursor.execute("SELECT AVG(sensor_current) as avg_temp FROM sensors WHERE device_id = %s AND sensor_class = 'temperature'", (device_id,))
            result = cursor.fetchone()
            return round(result['avg_temp']) if result and result['avg_temp'] is not None else 'N/A'
    except Exception as exc:
        logging.error('get_temp query failed: %s', exc)
        return 'N/A'
    finally:
        try:
            conn.close()
        except Exception:
            pass

def normalize_status(status):
    return status in (1, True, '1', 'true', 'True')

def build_health_warnings(cpu, mem, temp, online):
    warnings = []
    if not online:
        warnings.append('Offline')
    if isinstance(cpu, (int, float)) and cpu >= ALERT_THRESHOLDS['cpu_high']:
        warnings.append('High CPU')
    if isinstance(mem, (int, float)) and mem >= ALERT_THRESHOLDS['mem_high']:
        warnings.append('High Memory')
    if isinstance(temp, (int, float)) and temp >= ALERT_THRESHOLDS['temp_high']:
        warnings.append('High Temp')
    return warnings


def reduce_alerts(alerts):
    seen = set()
    reduced = []
    for alert in alerts:
        key = (
            str(alert.get('rule_id') or alert.get('name') or alert.get('id', '')),
            str(alert.get('hostname') or alert.get('device') or alert.get('device_id', '')).strip().lower()
        )
        if key in seen:
            continue
        seen.add(key)
        reduced.append(alert)
    return reduced


def db_connect():
    conn = sqlite3.connect(APP_DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_app_db():
    db_dir = os.path.dirname(APP_DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    conn = db_connect()
    try:
        conn.execute(
            '''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            '''
        )
        conn.commit()
    finally:
        conn.close()


def query_db(query, args=(), one=False):
    conn = db_connect()
    try:
        cur = conn.execute(query, args)
        rows = cur.fetchall()
        return rows[0] if one and rows else (rows if not one else None)
    finally:
        conn.close()


def execute_db(query, args=()):
    conn = db_connect()
    try:
        cur = conn.execute(query, args)
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def load_users():
    try:
        rows = query_db('SELECT username, role, created_at FROM users ORDER BY username ASC')
        return [dict(row) for row in rows]
    except Exception as exc:
        logging.error('Unable to load users from DB: %s', exc)
        return []


def get_user(username):
    username = str(username).strip()
    if not username:
        return None
    row = query_db('SELECT username, password_hash, role, created_at FROM users WHERE LOWER(username) = LOWER(?)', (username,), one=True)
    return dict(row) if row else None


def create_user(username, password, role='user'):
    username = str(username).strip()
    if not username or not password:
        return None
    if get_user(username):
        return None
    password_hash = generate_password_hash(password)
    created_at = datetime.now(timezone.utc).isoformat()
    role = role if role in ('admin', 'user') else 'user'
    try:
        execute_db(
            'INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, ?, ?)',
            (username, password_hash, role, created_at)
        )
        return get_user(username)
    except Exception as exc:
        logging.error('Unable to create user: %s', exc)
        return None


def update_user_password(username, password):
    username = str(username).strip()
    if not username or not password:
        return False
    if not get_user(username):
        return False
    try:
        execute_db(
            'UPDATE users SET password_hash = ? WHERE LOWER(username) = LOWER(?)',
            (generate_password_hash(password), username)
        )
        return True
    except Exception as exc:
        logging.error('Unable to update user password: %s', exc)
        return False


def delete_user(username):
    username = str(username).strip()
    if not username:
        return False
    try:
        changes = execute_db('DELETE FROM users WHERE LOWER(username) = LOWER(?)', (username,))
        return changes > 0
    except Exception as exc:
        logging.error('Unable to delete user: %s', exc)
        return False


def ensure_admin_user():
    users = load_users()
    if users:
        return
    if ADMIN_USERNAME and ADMIN_PASSWORD:
        if create_user(ADMIN_USERNAME, ADMIN_PASSWORD, role='admin'):
            logging.info('Admin account created from environment variables.')


def login_required(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        if 'user' not in session:
            return redirect(url_for('login'))
        return func(*args, **kwargs)
    return wrapper


def admin_required(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        if 'user' not in session or session.get('role') != 'admin':
            return redirect(url_for('login'))
        return func(*args, **kwargs)
    return wrapper


def current_user():
    username = session.get('user')
    if not username:
        return None
    return get_user(username)


@app.context_processor
def inject_user():
    user = current_user()
    return {
        'current_user': user,
        'is_admin': user is not None and user.get('role') == 'admin'
    }


@app.before_request
def require_login():
    if request.endpoint in ('login', 'setup', 'health', 'telegram_webhook', 'static'):
        return
    if request.endpoint is None:
        return
    if 'user' not in session:
        return redirect(url_for('login'))


init_app_db()
ensure_admin_user()

TELEGRAM_UPDATE_OFFSET = 0
MUTE_ALERTS = False
DASHBOARD_CACHE = None
DASHBOARD_CACHE_TIMESTAMP = 0
LOGIN_ATTEMPTS = {}


def invalidate_dashboard_cache():
    global DASHBOARD_CACHE, DASHBOARD_CACHE_TIMESTAMP
    DASHBOARD_CACHE = None
    DASHBOARD_CACHE_TIMESTAMP = 0


def get_client_ip():
    forwarded = request.headers.get('X-Forwarded-For', '')
    if forwarded:
        return forwarded.split(',')[0].strip()
    return request.remote_addr or 'unknown'


def load_dashboard_data(force=False):
    global DASHBOARD_CACHE, DASHBOARD_CACHE_TIMESTAMP
    if not force and DASHBOARD_CACHE is not None and (time.time() - DASHBOARD_CACHE_TIMESTAMP) < DASHBOARD_CACHE_TTL:
        return DASHBOARD_CACHE

    api_error = None
    raw_devices = []
    devices = []

    try:
        dev_res = LIBRENMS_SESSION.get(f"{LIBRENMS_URL}/devices", timeout=API_TIMEOUT)
        dev_res.raise_for_status()
        dev_data = dev_res.json()
        if dev_data.get('status') == 'ok':
            raw_devices = dev_data.get('devices', [])
        else:
            api_error = f"LibreNMS devices response status not ok: {dev_data}"
            logging.error(api_error)
    except Exception as exc:
        api_error = f"LibreNMS devices API fetch failed: {exc}"
        logging.error(api_error)

    if raw_devices:
        max_workers = min(8, len(raw_devices), (os.cpu_count() or 1) * 2)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            devices = list(executor.map(get_device_health, raw_devices))

    for device in devices:
        device['uptime_short'] = format_uptime(device.get('uptime', 0))
        device['online'] = normalize_status(device.get('status'))
        device['status_label'] = 'ONLINE' if device['online'] else 'OFFLINE'
        device['status_class'] = 'online' if device['online'] else 'offline'
        device['health_warnings'] = build_health_warnings(device['cpu'], device['mem'], device['temp'], device['online'])
        device['last_polled'] = device.get('last_polled') or device.get('last_poll') or 'N/A'
        device['display_name'] = device.get('display') or device.get('hostname')

    devices.sort(key=lambda x: (not x['online'], len(x['health_warnings']), -x['cpu'], -x['mem']))
    total = len(devices)
    up = sum(1 for d in devices if d['online'])
    down = total - up

    alerts = []
    try:
        alert_res = LIBRENMS_SESSION.get(f"{LIBRENMS_URL}/alerts", timeout=API_TIMEOUT)
        alert_res.raise_for_status()
        alert_data = alert_res.json()
        if alert_data.get('status') == 'ok':
            alerts = reduce_alerts(alert_data.get('alerts', []))[:18]
        else:
            if api_error is None:
                api_error = f"LibreNMS alerts response status not ok: {alert_data}"
            logging.error(api_error)
    except Exception as exc:
        if api_error is None:
            api_error = f"LibreNMS alerts API fetch failed: {exc}"
        logging.error(api_error)

    severity_counts = {'critical': 0, 'warning': 0, 'info': 0, 'other': 0}
    for alert in alerts:
        alert['id'] = alert.get('id')
        severity = str(alert.get('severity', 'other')).lower()
        if severity not in severity_counts:
            severity = 'other'
        severity_counts[severity] += 1
        alert['severity'] = severity
        alert['severity_label'] = severity.upper()
        alert['severity_class'] = (
            'bg-red-600 text-red-100' if severity == 'critical' else
            'bg-amber-600 text-amber-100' if severity == 'warning' else
            'bg-sky-600 text-sky-100' if severity == 'info' else
            'bg-slate-600 text-slate-100'
        )
        alert['display_name'] = alert.get('name') or alert.get('rule_id') or 'Alert'
        alert['timestamp'] = alert.get('timestamp', 'N/A')

    alert_order = {'critical': 0, 'warning': 1, 'info': 2, 'other': 3}
    alerts.sort(key=lambda a: (alert_order.get(a.get('severity'), 3), a.get('timestamp', '')))

    data = (devices, up, down, total, alerts, severity_counts, api_error)
    DASHBOARD_CACHE = data
    DASHBOARD_CACHE_TIMESTAMP = time.time()
    return data


def split_telegram_message(text, limit=3800):
    if not text:
        return []
    chunks = []
    while len(text) > limit:
        split_at = text.rfind('\n', 0, limit)
        if split_at < limit // 2:
            split_at = limit
        chunks.append(text[:split_at].strip())
        text = text[split_at:].strip()
    if text:
        chunks.append(text)
    return chunks


def send_telegram_message(text, chat_id=None):
    if not TELEGRAM_API_URL or not text:
        logging.warning('Telegram bot token or API URL missing.')
        return False
    destination = chat_id or TELEGRAM_CHAT_ID
    if not destination:
        logging.warning('Telegram chat ID missing.')
        return False

    success = True
    for chunk in split_telegram_message(str(text)):
        payload = {
            'chat_id': destination,
            'text': chunk,
            'disable_web_page_preview': True
        }
        try:
            resp = TELEGRAM_SESSION.post(f"{TELEGRAM_API_URL}/sendMessage", json=payload, timeout=10)
            if resp.status_code != 200:
                logging.warning('Telegram send failed: %s', resp.text)
                success = False
        except Exception as exc:
            logging.error('Telegram send exception: %s', exc)
            success = False
    return success


def get_devices_list():
    try:
        resp = LIBRENMS_SESSION.get(f"{LIBRENMS_URL}/devices", timeout=API_TIMEOUT)
        data = resp.json()
        if data.get('status') == 'ok':
            return data.get('devices', [])
    except Exception as exc:
        logging.debug('get_devices_list failed: %s', exc)
    return []


def find_device_by_id(device_id):
    try:
        resp = LIBRENMS_SESSION.get(f"{LIBRENMS_URL}/devices/{device_id}", timeout=API_TIMEOUT)
        data = resp.json()
        if data.get('status') == 'ok':
            return data.get('device') or {}
    except Exception as exc:
        logging.debug('find_device_by_id failed: %s', exc)
    return None


def find_device_by_identifier(identifier):
    normalized = str(identifier).strip().lower()
    if not normalized:
        return None

    if normalized.isdigit():
        device = find_device_by_id(normalized)
        if device:
            return device

    devices = get_devices_list()
    if not devices:
        return None

    exact_keys = ['hostname', 'sysName', 'display', 'ip', 'ip_netmask']
    for device in devices:
        for key in exact_keys:
            value = str(device.get(key, '') or '').strip().lower()
            if value == normalized:
                return device

    for device in devices:
        for key in exact_keys:
            value = str(device.get(key, '') or '').strip().lower()
            if normalized in value:
                return device

    return None


def get_device_history(device_id, hours=24, interval_minutes=30):
    try:
        conn = pymysql.connect(
            host=DB_HOST,
            user=DB_USER,
            password=DB_PASSWORD,
            db=DB_NAME,
            cursorclass=pymysql.cursors.DictCursor,
        )
        with conn.cursor() as cursor:
            cursor.execute("SHOW COLUMNS FROM processors LIKE 'timestamp'")
            has_timestamp = cursor.fetchone() is not None
            if has_timestamp:
                cursor.execute(
                    """
                    SELECT
                        FLOOR(UNIX_TIMESTAMP(`timestamp`) / %s) * %s AS bucket,
                        AVG(processor_usage) AS avg_cpu
                    FROM processors
                    WHERE device_id = %s
                      AND `timestamp` >= NOW() - INTERVAL %s HOUR
                    GROUP BY bucket
                    ORDER BY bucket
                    """,
                    (interval_minutes * 60, interval_minutes * 60, device_id, hours)
                )
                rows = cursor.fetchall()
                timestamps = []
                values = []
                for row in rows:
                    bucket = row.get('bucket')
                    avg_cpu = row.get('avg_cpu')
                    if bucket is None or avg_cpu is None:
                        continue
                    timestamps.append(time.strftime('%H:%M', time.localtime(int(bucket))))
                    values.append(round(float(avg_cpu), 1))
                return [{'timestamp': ts, 'cpu': val} for ts, val in zip(timestamps, values)]
            cursor.execute("SELECT AVG(processor_usage) as avg_cpu FROM processors WHERE device_id = %s", (device_id,))
            row = cursor.fetchone()
            avg_cpu = row.get('avg_cpu') if row else None
            if avg_cpu is None:
                return []
            return [{'timestamp': datetime.now(timezone.utc).strftime('%H:%M'), 'cpu': round(float(avg_cpu), 1)}]
    except Exception as exc:
        logging.error('get_device_history query failed: %s', exc)
        return []
    finally:
        try:
            conn.close()
        except Exception:
            pass


def format_device_message(device):
    cpu = get_cpu(device['device_id']) if device else 'N/A'
    mem = get_mem(device['device_id']) if device else 'N/A'
    temp = get_temp(device['device_id']) if device else 'N/A'
    online = normalize_status(device.get('status')) if device else False
    return (
        f"Device: {device.get('hostname', 'N/A')}\n"
        f"ID: {device.get('device_id', 'N/A')}\n"
        f"OS: {device.get('os', 'N/A')}\n"
        f"Status: {'ONLINE' if online else 'OFFLINE'}\n"
        f"CPU: {cpu}%\n"
        f"Memory: {mem}%\n"
        f"Temp: {temp if temp != 'N/A' else 'N/A'}°C\n"
        f"Last Polled: {device.get('last_polled', 'N/A')}\n"
    )


def ask_openai(system_prompt, user_prompt):
    if not OPENAI_API_KEY:
        return None, 'OpenAI API key is not configured.'
    payload = {
        'model': OPENAI_MODEL,
        'messages': [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_prompt}
        ],
        'max_tokens': 450,
        'temperature': 0.7,
    }
    headers = {
        'Authorization': f'Bearer {OPENAI_API_KEY}',
        'Content-Type': 'application/json'
    }
    try:
        resp = requests.post(OPENAI_API_URL, headers=headers, json=payload, timeout=(5, 30))
        resp.raise_for_status()
        data = resp.json()
        choice = data.get('choices', [{}])[0]
        content = choice.get('message', {}).get('content')
        if content:
            return content.strip(), None
        return None, 'OpenAI returned no answer.'
    except Exception as exc:
        logging.error('OpenAI request failed: %s', exc)
        return None, str(exc)


def summarize_current_status():
    devices, up, down, total, alerts, severity_counts, api_error = load_dashboard_data()
    lines = [
        f'Devices: {total}',
        f'Online: {up}',
        f'Offline: {down}',
        f'Alerts: {len(alerts)}',
        f'Critical: {severity_counts.get("critical", 0)}',
        f'Warning: {severity_counts.get("warning", 0)}',
        f'Info: {severity_counts.get("info", 0)}',
        f'Other: {severity_counts.get("other", 0)}',
    ]
    if api_error:
        lines.append(f'API errors: {api_error}')

    report = '\n'.join(lines)
    report += '\n\nTop devices and alerts are summarized from the live dashboard.'
    return report, devices, alerts


def handle_telegram_command(text, chat_id):
    global MUTE_ALERTS
    command = text.strip().split()
    if not command:
        send_telegram_message('Please send a valid command. Use /help for a list of commands.', chat_id)
        return
    cmd = command[0].lower()

    if cmd == '/help':
        send_telegram_message(
            'Available commands:\n'
            '/status - Current device and alert summary\n'
            '/alerts - Active alert list\n'
            '/device <id|ip|hostname> - Details for a specific device\n'
            '/reboot_check <id|ip|hostname> - Check if device recently rebooted\n'
            '/mute_alerts - Toggle alert delivery\n'
            '/ack_all - Acknowledge all current alerts\n'
            '/ai <question|status|report> - Ask AI for a network report or answer\n'
            '/help - Show this message',
            chat_id
        )
        return

    if cmd == '/mute_alerts':
        MUTE_ALERTS = not MUTE_ALERTS
        send_telegram_message(f'Alerts muted: {MUTE_ALERTS}', chat_id)
        return

    if MUTE_ALERTS:
        send_telegram_message('Alerts are currently muted. Use /mute_alerts to toggle.', chat_id)
        return

    if cmd == '/status':
        devices, up, down, total, alerts, severity_counts, api_error = load_dashboard_data()
        send_telegram_message(
            f'Status Summary\nDevices: {up}/{total} online\nDown: {down}\nAlerts: {len(alerts)}',
            chat_id
        )
        return

    if cmd == '/alerts':
        _, _, _, _, alerts, severity_counts, api_error = load_dashboard_data()
        if not alerts:
            send_telegram_message('No active alerts at this time.', chat_id)
            return
        message = 'Active Alerts:\n' + '\n'.join(
            f"{idx+1}. [{a['severity_label']}] {a['display_name']} ({a.get('hostname','N/A')})"
            for idx, a in enumerate(alerts[:8])
        )
        send_telegram_message(message, chat_id)
        return

    if cmd == '/ai':
        query = ' '.join(command[1:]).strip()
        if not query:
            send_telegram_message('Usage: /ai <question> or /ai status or /ai report', chat_id)
            return

        if query.lower() in ('status', 'report', 'summary'):
            summary, devices, alerts = summarize_current_status()
            system_prompt = 'You are a network monitoring analyst. Provide a concise status report and suggested next actions.'
            user_prompt = f"Here is the current LibreNMS summary:\n{summary}\nRespond with a short, actionable operations report."
        else:
            system_prompt = 'You are a helpful network operations assistant.'
            user_prompt = query

        ai_response, ai_error = ask_openai(system_prompt, user_prompt)
        if ai_error:
            send_telegram_message(f'AI error: {ai_error}', chat_id)
            return
        send_telegram_message(ai_response, chat_id)
        return

    if cmd == '/device' and len(command) > 1:
        device_id = ' '.join(command[1:])
        device = find_device_by_identifier(device_id)
        if not device:
            send_telegram_message(f'Device {device_id} not found.', chat_id)
            return
        send_telegram_message(format_device_message(device), chat_id)
        return

    if cmd == '/reboot_check' and len(command) > 1:
        identifier = ' '.join(command[1:])
        device = find_device_by_identifier(identifier)
        if not device:
            send_telegram_message('Device not found', chat_id)
            return
        uptime = format_uptime(device.get('uptime', 0))
        online = normalize_status(device.get('status'))
        send_telegram_message(
            f'Reboot Check:\n'
            f'{device.get("hostname", "Unknown")}\n'
            f"Status: {'ONLINE' if online else 'OFFLINE'}\n"
            f'Uptime: {uptime}',
            chat_id
        )
        return

    if cmd == '/ack_all':
        _, _, _, _, alerts, severity_counts, api_error = load_dashboard_data()
        success = 0
        for a in alerts:
            try:
                response = requests.put(f"{LIBRENMS_URL}/alerts/{a['id']}", headers=HEADERS, json={'state': 2}, timeout=5)
                if response.status_code == 200:
                    success += 1
            except Exception:
                pass
        send_telegram_message(f'Acknowledged {success} alerts', chat_id)
        return

    send_telegram_message('Unknown command. Use /help to see available commands.', chat_id)


def poll_telegram_updates():
    global TELEGRAM_UPDATE_OFFSET
    if not TELEGRAM_API_URL:
        logging.warning('Telegram polling disabled: no API URL available.')
        return

    logging.info('Starting Telegram update poller.')
    while True:
        try:
            params = {'timeout': 20, 'offset': TELEGRAM_UPDATE_OFFSET + 1}
            resp = TELEGRAM_SESSION.get(f"{TELEGRAM_API_URL}/getUpdates", params=params, timeout=30)
            data = resp.json()
            if data.get('ok'):
                for update in data.get('result', []):
                    TELEGRAM_UPDATE_OFFSET = update['update_id']
                    message = update.get('message') or update.get('edited_message')
                    if not message:
                        continue
                    chat = message.get('chat', {})
                    text = message.get('text', '')
                    if not text:
                        continue
                    if TELEGRAM_CHAT_ID and str(chat.get('id')) != str(TELEGRAM_CHAT_ID):
                        logging.info('Ignoring Telegram message from chat %s', chat.get('id'))
                        continue
                    handle_telegram_command(text, chat.get('id'))
        except Exception as exc:
            logging.error('Telegram poll exception: %s', exc)
            time.sleep(5)


def get_device_health(device):
    """ Device တစ်ခုချင်းစီ၏ Detail data ကို API ထပ်ခေါ်ခြင်း """
    try:
        d_id = device.get('device_id')
        if d_id is None:
            raise ValueError('Missing device_id')
        device['cpu'] = get_cpu(d_id)
        device['mem'] = get_mem(d_id)
        device['temp'] = get_temp(d_id)
        return device
    except Exception as exc:
        logging.debug('get_device_health failed for device %s: %s', device.get('hostname', '<unknown>'), exc)
        device['cpu'] = 0
        device['mem'] = 0
        device['temp'] = 'N/A'
        return device


@app.route('/setup', methods=['GET', 'POST'])
@app.route('/setup/', methods=['GET', 'POST'])
def setup():
    if load_users():
        return redirect(url_for('login'))
    error = None
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        password_confirm = request.form.get('password_confirm', '')
        if not username or not password:
            error = 'Username and password are required.'
        elif password != password_confirm:
            error = 'Passwords do not match.'
        elif get_user(username):
            error = 'Username is already taken.'
        else:
            create_user(username, password, role='admin')
            session['user'] = username
            session['role'] = 'admin'
            return redirect(url_for('index'))
    return render_template('setup.html', error=error)


@app.route('/login', methods=['GET', 'POST'])
@app.route('/login/', methods=['GET', 'POST'])
def login():
    if 'user' in session:
        return redirect(url_for('index'))
    error = None
    client_ip = get_client_ip()
    login_data = LOGIN_ATTEMPTS.setdefault(client_ip, {'count': 0, 'first': time.time()})
    if login_data['count'] >= MAX_LOGIN_ATTEMPTS and time.time() - login_data['first'] < LOGIN_LOCKOUT_SECONDS:
        error = 'Too many login attempts. Try again later.'
        return render_template('login.html', error=error, setup_mode=not bool(load_users()))

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        user = get_user(username)
        if not user or not check_password_hash(user.get('password_hash', ''), password):
            login_data['count'] += 1
            if login_data['count'] == 1:
                login_data['first'] = time.time()
            error = 'Invalid username or password.'
        else:
            LOGIN_ATTEMPTS.pop(client_ip, None)
            session['user'] = user['username']
            session['role'] = user.get('role', 'user')
            session.permanent = True
            return redirect(url_for('index'))
    return render_template('login.html', error=error, setup_mode=not bool(load_users()))


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    error = None
    message = None
    if request.method == 'POST':
        current_password = request.form.get('current_password', '')
        new_password = request.form.get('new_password', '')
        confirm_password = request.form.get('confirm_password', '')
        user = current_user()
        if not user or not check_password_hash(user.get('password_hash', ''), current_password):
            error = 'Current password is incorrect.'
        elif not new_password:
            error = 'New password cannot be empty.'
        elif new_password != confirm_password:
            error = 'Password confirmation does not match.'
        else:
            update_user_password(user['username'], new_password)
            message = 'Password updated successfully.'
    return render_template('profile.html', error=error, message=message)


@app.route('/admin/users', methods=['GET', 'POST'])
@admin_required
def admin_users():
    message = None
    error = None
    users = load_users()
    if request.method == 'POST':
        action = request.form.get('action', 'create')
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        role = request.form.get('role', 'user')

        if action == 'create':
            if not username or not password:
                error = 'Username and password are required.'
            elif get_user(username):
                error = 'Username already exists.'
            else:
                create_user(username, password, role=role)
                message = f'User {username} created successfully.'
                users = load_users()
        elif action == 'reset':
            if not username or not password:
                error = 'Username and new password are required.'
            elif not get_user(username):
                error = 'User not found.'
            else:
                update_user_password(username, password)
                message = f'Password for {username} has been reset.'
        elif action == 'delete':
            if not username:
                error = 'Username is required to delete.'
            elif username.lower() == session.get('user', '').lower():
                error = 'You cannot delete your own account while logged in.'
            elif not delete_user(username):
                error = 'Unable to delete user.'
            else:
                message = f'User {username} deleted successfully.'
                users = load_users()
        else:
            error = 'Unknown action.'

    return render_template('admin_users.html', users=users, message=message, error=error)


@app.errorhandler(404)
def page_not_found(e):
    if 'user' in session:
        return redirect(url_for('index'))
    return redirect(url_for('login'))


@app.route('/')
def index():
    logging.info("Dashboard accessed")
    try:
        devices, up, down, total, alerts, severity_counts, api_error = load_dashboard_data()
        return render_template('index.html', devices=devices, up=up, down=down, total=total, alerts=alerts, severity_counts=severity_counts, thresholds=ALERT_THRESHOLDS, api_error=api_error)
    except Exception as e:
        logging.error(f"Backend error: {str(e)}")
        return f"Backend Error: {str(e)}"


def build_report_summary(devices, alerts, severity_counts):
    top_cpu = sorted([d for d in devices if isinstance(d.get('cpu'), (int, float))], key=lambda x: x['cpu'], reverse=True)[:8]
    top_mem = sorted([d for d in devices if isinstance(d.get('mem'), (int, float))], key=lambda x: x['mem'], reverse=True)[:8]
    return {
        'total_devices': len(devices),
        'online': sum(1 for d in devices if d.get('online')),
        'offline': sum(1 for d in devices if not d.get('online')),
        'alert_count': len(alerts),
        'severity_counts': severity_counts,
        'top_cpu': top_cpu,
        'top_mem': top_mem,
        'recent_alerts': alerts[:12]
    }


@app.route('/reports')
@login_required
def reports():
    devices, up, down, total, alerts, severity_counts, api_error = load_dashboard_data()
    report = build_report_summary(devices, alerts, severity_counts)
    return render_template('reports.html', report=report, api_error=api_error)


@app.route('/reports/export')
@login_required
def export_reports():
    devices, up, down, total, alerts, severity_counts, api_error = load_dashboard_data()
    report_type = request.args.get('type', 'devices').lower()
    output = StringIO()
    writer = csv.writer(output)
    if report_type == 'alerts':
        writer.writerow(['ID', 'Severity', 'Hostname', 'Message', 'Timestamp'])
        for alert in alerts:
            writer.writerow([
                alert.get('id', ''),
                alert.get('severity_label', ''),
                alert.get('hostname', ''),
                alert.get('display_name', ''),
                alert.get('timestamp', '')
            ])
        filename = 'alerts_report.csv'
    else:
        writer.writerow(['Device ID', 'Device', 'Status', 'CPU', 'Mem', 'Temp', 'Uptime', 'Health warnings'])
        for device in devices:
            writer.writerow([
                device.get('device_id', ''),
                device.get('display_name', ''),
                'ONLINE' if device.get('online') else 'OFFLINE',
                device.get('cpu', ''),
                device.get('mem', ''),
                device.get('temp', ''),
                device.get('uptime_short', ''),
                '; '.join(device.get('health_warnings', []))
            ])
        filename = 'device_report.csv'
    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={
            'Content-Disposition': f'attachment; filename="{filename}"'
        }
    )


def set_telegram_webhook():
    if not TELEGRAM_API_URL or not TELEGRAM_WEBHOOK_URL:
        return False
    try:
        resp = TELEGRAM_SESSION.post(
            f"{TELEGRAM_API_URL}/setWebhook",
            json={'url': TELEGRAM_WEBHOOK_URL},
            timeout=API_TIMEOUT
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get('ok'):
            logging.info('Telegram webhook set to %s', TELEGRAM_WEBHOOK_URL)
            return True
        logging.warning('Telegram webhook setup failed: %s', data)
    except Exception as exc:
        logging.error('Telegram webhook setup exception: %s', exc)
    return False


def start_telegram_listener():
    if not TELEGRAM_API_URL or not TELEGRAM_CHAT_ID:
        logging.info('Telegram listener not started because TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing.')
        return
    if TELEGRAM_WEBHOOK_URL:
        if set_telegram_webhook():
            logging.info('Telegram webhook configured; poller disabled.')
            return
        logging.warning('Telegram webhook configuration failed; falling back to polling.')
    thread = threading.Thread(target=poll_telegram_updates, daemon=True)
    thread.start()
    logging.info('Telegram listener started.')

@app.route('/telegram', methods=['POST'])
def telegram_webhook():
    if not TELEGRAM_API_URL:
        return jsonify({'ok': False, 'error': 'Telegram bot token not configured'}), 400
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'ok': False, 'error': 'invalid json'}), 400
    message = data.get('message') or data.get('edited_message') or data.get('callback_query', {}).get('message')
    if not message:
        return jsonify({'ok': True})
    chat = message.get('chat', {})
    text = message.get('text', '')
    if not text:
        return jsonify({'ok': True})
    if TELEGRAM_CHAT_ID and str(chat.get('id')) != str(TELEGRAM_CHAT_ID):
        logging.info('Ignoring Telegram message from chat %s', chat.get('id'))
        return jsonify({'ok': True})
    handle_telegram_command(text, chat.get('id'))
    return jsonify({'ok': True})


@app.route('/device/<int:device_id>/history')
def device_history(device_id):
    series = get_device_history(device_id)
    device = find_device_by_id(device_id) or {}
    return jsonify({
        'device_id': device_id,
        'device_name': device.get('display') or device.get('hostname', f'Device {device_id}'),
        'timestamps': [item['timestamp'] for item in series],
        'values': [item['cpu'] for item in series],
        'points': len(series)
    })


@app.route('/acknowledge_all', methods=['POST'])
def acknowledge_all():
    logging.info('Acknowledging all visible alerts')
    try:
        _, _, _, _, alerts, _, _ = load_dashboard_data(force=True)
        success = 0
        for alert in alerts:
            if not alert.get('id'):
                continue
            try:
                response = requests.put(
                    f"{LIBRENMS_URL}/alerts/{alert['id']}",
                    headers=HEADERS,
                    json={'state': 2},
                    timeout=10
                )
                if response.status_code == 200:
                    success += 1
            except Exception as exc:
                logging.debug('Failed to acknowledge alert %s: %s', alert.get('id'), exc)
        invalidate_dashboard_cache()
        logging.info('Acknowledged %s alerts', success)
    except Exception as exc:
        logging.error('Error acknowledging all alerts: %s', exc)
    return redirect(url_for('index'))


@app.route('/acknowledge/<int:alert_id>', methods=['POST'])
def acknowledge_alert(alert_id):
    logging.info(f"Acknowledging alert {alert_id}")
    try:
        response = requests.put(f"{LIBRENMS_URL}/alerts/{alert_id}", headers=HEADERS, json={"state": 2}, timeout=10)
        if response.status_code == 200:
            logging.info(f"Alert {alert_id} acknowledged successfully")
            invalidate_dashboard_cache()
        else:
            logging.warning(f"Failed to acknowledge alert {alert_id}: {response.text}")
        return redirect(url_for('index'))
    except Exception as e:
        logging.error(f"Error acknowledging alert {alert_id}: {str(e)}")
        return redirect(url_for('index'))

@app.route('/health')
def health():
    return {'status': 'ok'}

if __name__ == '__main__':
    start_telegram_listener()
    base_port = int(os.getenv('PORT', '8000'))
    max_tries = 10
    for i in range(max_tries):
        port = base_port + i
        try:
            logging.info(f"Starting Flask app on port {port}")
            app.run(host='0.0.0.0', port=port)
            break
        except OSError as e:
            logging.error(f"Port {port} unavailable: {e}")
            if i == max_tries - 1:
                logging.error('All ports unavailable. Exiting.')
                sys.exit(1)
