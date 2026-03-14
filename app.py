import os
import subprocess
import threading
import zipfile
import json
import re
import signal
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from flask_sock import Sock
from werkzeug.utils import secure_filename
from functools import wraps

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'change-this-secret-key-123')
sock = Sock(app)

UPLOAD_FOLDER = 'uploads'
BOTS_FOLDER = 'bots'
PASSWORD = os.environ.get('DASHBOARD_PASSWORD', 'admin123')
GITHUB_TOKEN = os.environ.get('GITHUB_TOKEN', '')
GITHUB_REPO = os.environ.get('GITHUB_REPO', '')
RELEASE_TAG = 'bot-storage'

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(BOTS_FOLDER, exist_ok=True)

running_bots = {}
bot_logs = {}
bot_status = {}
bot_qr = {}
qr_subscribers = {}

# ── GitHub Releases Storage ───────────────────────────────

def gh_headers():
    return {
        'Authorization': f'token {GITHUB_TOKEN}',
        'Accept': 'application/vnd.github.v3+json',
        'Content-Type': 'application/json'
    }

def get_release():
    """Get or create the bot-storage release"""
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return None
    try:
        url = f'https://api.github.com/repos/{GITHUB_REPO}/releases/tags/{RELEASE_TAG}'
        req = urllib.request.Request(url, headers=gh_headers())
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return create_release()
        return None
    except:
        return None

def create_release():
    """Create the bot-storage release on GitHub"""
    try:
        url = f'https://api.github.com/repos/{GITHUB_REPO}/releases'
        data = json.dumps({
            'tag_name': RELEASE_TAG,
            'name': 'Bot Storage',
            'body': 'Persistent bot storage for BotDash',
            'draft': False,
            'prerelease': False
        }).encode()
        req = urllib.request.Request(url, data=data, headers=gh_headers(), method='POST')
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())
    except:
        return None

def upload_bot_to_github(bot_name, zip_path):
    """Upload bot ZIP to GitHub Releases"""
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return False
    try:
        release = get_release()
        if not release:
            return False

        # Delete existing asset with same name if exists
        for asset in release.get('assets', []):
            if asset['name'] == f'{bot_name}.zip':
                del_url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/assets/{asset['id']}"
                req = urllib.request.Request(del_url, headers=gh_headers(), method='DELETE')
                try:
                    urllib.request.urlopen(req)
                except:
                    pass

        # Upload new asset
        upload_url = release['upload_url'].replace('{?name,label}', '')
        with open(zip_path, 'rb') as f:
            data = f.read()

        headers = {
            'Authorization': f'token {GITHUB_TOKEN}',
            'Content-Type': 'application/zip',
            'Accept': 'application/vnd.github.v3+json'
        }
        req = urllib.request.Request(
            f'{upload_url}?name={bot_name}.zip',
            data=data, headers=headers, method='POST'
        )
        with urllib.request.urlopen(req) as r:
            return r.status == 201
    except Exception as e:
        print(f'GitHub upload error: {e}')
        return False

def delete_bot_from_github(bot_name):
    """Delete bot ZIP from GitHub Releases"""
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return
    try:
        release = get_release()
        if not release:
            return
        for asset in release.get('assets', []):
            if asset['name'] == f'{bot_name}.zip':
                del_url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/assets/{asset['id']}"
                req = urllib.request.Request(del_url, headers=gh_headers(), method='DELETE')
                urllib.request.urlopen(req)
    except:
        pass

def restore_bots_from_github():
    """On startup, download all bots from GitHub Releases"""
    if not GITHUB_TOKEN or not GITHUB_REPO:
        print('No GitHub token — skipping restore')
        return
    print('Restoring bots from GitHub Releases...')
    try:
        release = get_release()
        if not release:
            print('No release found')
            return
        assets = release.get('assets', [])
        print(f'Found {len(assets)} bots to restore')
        for asset in assets:
            if not asset['name'].endswith('.zip'):
                continue
            bot_name = asset['name'].replace('.zip', '')
            bot_dir = Path(BOTS_FOLDER) / bot_name
            if bot_dir.exists():
                print(f'  {bot_name} already exists, skipping')
                continue
            print(f'  Restoring {bot_name}...')
            try:
                zip_path = Path(UPLOAD_FOLDER) / asset['name']
                headers = {
                    'Authorization': f'token {GITHUB_TOKEN}',
                    'Accept': 'application/octet-stream'
                }
                req = urllib.request.Request(asset['browser_download_url'], headers=headers)
                with urllib.request.urlopen(req) as r:
                    zip_path.write_bytes(r.read())

                bot_dir.mkdir(exist_ok=True)
                extract_zip(zip_path, bot_dir)
                install_deps(bot_dir)
                print(f'  ✅ {bot_name} restored')
            except Exception as e:
                print(f'  ❌ Failed to restore {bot_name}: {e}')
    except Exception as e:
        print(f'Restore error: {e}')

def extract_zip(zip_path, bot_dir):
    with zipfile.ZipFile(zip_path, 'r') as z:
        names = z.namelist()
        tops = set(n.split('/')[0] for n in names)
        prefix = ''
        if len(tops) == 1:
            top = list(tops)[0]
            if all(n.startswith(top + '/') or n == top + '/' for n in names):
                prefix = top + '/'
        for member in z.infolist():
            mp = member.filename
            if prefix:
                if not mp.startswith(prefix):
                    continue
                mp = mp[len(prefix):]
            if not mp or mp.endswith('/'):
                continue
            target = bot_dir / mp
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(member) as src, open(target, 'wb') as dst:
                dst.write(src.read())

def install_deps(bot_dir):
    bot_type = detect_bot_type(bot_dir)
    if bot_type == 'python' and (bot_dir / 'requirements.txt').exists():
        subprocess.run(['pip', 'install', '-r', 'requirements.txt', '-q'],
                      cwd=bot_dir, capture_output=True, timeout=180)
    elif bot_type == 'node' and (bot_dir / 'package.json').exists():
        subprocess.run(['npm', 'install', '--silent', '--legacy-peer-deps'],
                      cwd=bot_dir, capture_output=True, timeout=300)

# ── Bot Helpers ───────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

def detect_bot_type(bot_dir):
    if (bot_dir / 'package.json').exists():
        return 'node'
    for _ in bot_dir.rglob('*.py'):
        return 'python'
    return 'unknown'

def find_entry_point(bot_dir, bot_type):
    if bot_type == 'python':
        for name in ['main.py', 'bot.py', 'index.py', 'app.py', 'run.py']:
            if (bot_dir / name).exists():
                return name
        pyfiles = list(bot_dir.glob('*.py'))
        return pyfiles[0].name if pyfiles else None
    elif bot_type == 'node':
        try:
            pkg = json.loads((bot_dir / 'package.json').read_text())
            if 'start' in pkg.get('scripts', {}):
                return '__npm_start__'
            main = pkg.get('main', 'index.js')
            if (bot_dir / main).exists():
                return main
        except:
            pass
        for name in ['index.js', 'bot.js', 'main.js', 'app.js']:
            if (bot_dir / name).exists():
                return name
    return None

def notify_ws(bot_name, data):
    dead = []
    for ws in qr_subscribers.get(bot_name, []):
        try:
            ws.send(json.dumps(data))
        except:
            dead.append(ws)
    for ws in dead:
        try:
            qr_subscribers[bot_name].remove(ws)
        except:
            pass

def is_qr_char_line(line):
    qr_chars = ['█', '▀', '▄', '■', '▌', '▐', '░', '▒', '▓', '◼', '◻']
    return any(c in line for c in qr_chars) and len(line.strip()) > 5

def stream_logs(bot_name, process):
    qr_buffer = []
    in_qr = False
    blank_count = 0

    def read_pipe(pipe):
        nonlocal qr_buffer, in_qr, blank_count
        for raw in iter(pipe.readline, b''):
            line = raw.decode('utf-8', errors='replace').rstrip()
            ts = datetime.now().strftime('%H:%M:%S')
            bot_logs.setdefault(bot_name, []).append(f"[{ts}] {line}")
            if len(bot_logs[bot_name]) > 500:
                bot_logs[bot_name] = bot_logs[bot_name][-500:]

            if is_qr_char_line(line):
                if not in_qr:
                    in_qr = True
                    qr_buffer = []
                    blank_count = 0
                qr_buffer.append(line)
                blank_count = 0
            elif in_qr:
                if not line.strip():
                    blank_count += 1
                    qr_buffer.append(line)
                    if blank_count >= 2 and len(qr_buffer) > 10:
                        qr_text = '\n'.join(qr_buffer)
                        bot_qr[bot_name] = qr_text
                        bot_status[bot_name] = 'waiting_qr'
                        notify_ws(bot_name, {'type': 'qr', 'qr': qr_text})
                        in_qr = False
                        qr_buffer = []
                else:
                    qr_buffer.append(line)
                    blank_count = 0

            lower = line.lower()
            if any(x in lower for x in ['connected', 'logged in', 'connection opened', 'restored', 'credentials saved']):
                bot_status[bot_name] = 'connected'
                bot_qr.pop(bot_name, None)
                notify_ws(bot_name, {'type': 'connected'})

            notify_ws(bot_name, {'type': 'log', 'line': f"[{ts}] {line}"})
        pipe.close()

    threading.Thread(target=read_pipe, args=(process.stdout,), daemon=True).start()
    threading.Thread(target=read_pipe, args=(process.stderr,), daemon=True).start()
    process.wait()
    ts = datetime.now().strftime('%H:%M:%S')
    code = process.returncode
    bot_logs.setdefault(bot_name, []).append(f"[{ts}] 🔴 Exited with code {code}")
    if bot_status.get(bot_name) not in ('connected',):
        bot_status[bot_name] = 'stopped' if code == 0 else 'error'
    running_bots.pop(bot_name, None)
    notify_ws(bot_name, {'type': 'stopped', 'code': code})

# ── Auth ──────────────────────────────────────────────────

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        if request.form.get('password') == PASSWORD:
            session['logged_in'] = True
            return redirect(url_for('index'))
        error = 'Wrong password'
    return render_template('login.html', error=error)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# ── Pages ─────────────────────────────────────────────────

@app.route('/')
@login_required
def index():
    bots = []
    p = Path(BOTS_FOLDER)
    if p.exists():
        for d in sorted(p.iterdir()):
            if d.is_dir():
                t = detect_bot_type(d)
                bots.append({
                    'name': d.name,
                    'type': t,
                    'entry': find_entry_point(d, t),
                    'status': bot_status.get(d.name, 'stopped'),
                })
    return render_template('index.html', bots=bots)

# ── API ───────────────────────────────────────────────────

@app.route('/upload', methods=['POST'])
@login_required
def upload():
    if 'file' not in request.files:
        return jsonify({'error': 'No file'}), 400
    file = request.files['file']
    if not file or not file.filename.endswith('.zip'):
        return jsonify({'error': 'Only ZIP files allowed'}), 400

    raw_name = secure_filename(file.filename).rsplit('.', 1)[0]
    # Remove double .zip if present
    if raw_name.endswith('.zip'):
        raw_name = raw_name[:-4]
    bot_name = re.sub(r'[^a-zA-Z0-9_-]', '_', raw_name).strip('_') or 'bot'

    zip_path = Path(UPLOAD_FOLDER) / f'{bot_name}.zip'
    file.save(zip_path)

    bot_dir = Path(BOTS_FOLDER) / bot_name
    bot_dir.mkdir(exist_ok=True)
    extract_zip(zip_path, bot_dir)
    install_deps(bot_dir)

    # Save to GitHub Releases for persistence
    gh_saved = upload_bot_to_github(bot_name, zip_path)

    bot_type = detect_bot_type(bot_dir)
    return jsonify({
        'success': True,
        'bot_name': bot_name,
        'type': bot_type,
        'saved_to_github': gh_saved,
        'install_log': ['✅ Dependencies installed', '✅ Saved to GitHub' if gh_saved else '⚠ GitHub save failed']
    })

@app.route('/bot/start/<bot_name>', methods=['POST'])
@login_required
def start_bot(bot_name):
    if bot_name in running_bots:
        return jsonify({'error': 'Already running'}), 400
    bot_dir = Path(BOTS_FOLDER) / bot_name
    if not bot_dir.exists():
        return jsonify({'error': 'Bot not found'}), 404
    bot_type = detect_bot_type(bot_dir)
    entry = find_entry_point(bot_dir, bot_type)
    if not entry:
        return jsonify({'error': 'No entry point found'}), 400

    data = request.get_json(silent=True) or {}
    if data.get('env'):
        (bot_dir / '.env').write_text(data['env'])

    cmd = ['python', '-u', entry] if bot_type == 'python' else \
          (['npm', 'start'] if entry == '__npm_start__' else ['node', entry])

    env = os.environ.copy()
    env_path = bot_dir / '.env'
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if '=' in line and not line.startswith('#'):
                k, v = line.split('=', 1)
                env[k.strip()] = v.strip()

    proc = subprocess.Popen(cmd, cwd=bot_dir, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=env, preexec_fn=os.setsid)
    running_bots[bot_name] = proc
    bot_status[bot_name] = 'starting'
    bot_logs[bot_name] = [f"[{datetime.now().strftime('%H:%M:%S')}] 🟢 Starting {bot_name}..."]
    threading.Thread(target=stream_logs, args=(bot_name, proc), daemon=True).start()
    return jsonify({'success': True})

@app.route('/bot/stop/<bot_name>', methods=['POST'])
@login_required
def stop_bot(bot_name):
    if bot_name not in running_bots:
        return jsonify({'error': 'Not running'}), 400
    proc = running_bots[bot_name]
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except:
        proc.terminate()
    bot_status[bot_name] = 'stopped'
    running_bots.pop(bot_name, None)
    bot_logs.setdefault(bot_name, []).append(f"[{datetime.now().strftime('%H:%M:%S')}] 🔴 Stopped")
    notify_ws(bot_name, {'type': 'stopped'})
    return jsonify({'success': True})

@app.route('/bot/delete/<bot_name>', methods=['POST'])
@login_required
def delete_bot(bot_name):
    if bot_name in running_bots:
        try:
            os.killpg(os.getpgid(running_bots[bot_name].pid), signal.SIGTERM)
        except:
            running_bots[bot_name].terminate()
        running_bots.pop(bot_name, None)
    import shutil
    bot_dir = Path(BOTS_FOLDER) / bot_name
    if bot_dir.exists():
        shutil.rmtree(bot_dir)
    for d in [bot_status, bot_logs, bot_qr]:
        d.pop(bot_name, None)
    # Also delete from GitHub
    threading.Thread(target=delete_bot_from_github, args=(bot_name,), daemon=True).start()
    return jsonify({'success': True})

@app.route('/bot/logs/<bot_name>')
@login_required
def get_logs(bot_name):
    logs = bot_logs.get(bot_name, [])
    since = request.args.get('since', 0, type=int)
    return jsonify({'logs': logs[since:], 'total': len(logs), 'status': bot_status.get(bot_name, 'stopped')})

@app.route('/bot/env/<bot_name>', methods=['GET', 'POST'])
@login_required
def bot_env(bot_name):
    env_path = Path(BOTS_FOLDER) / bot_name / '.env'
    if request.method == 'POST':
        env_path.write_text(request.json.get('env', ''))
        return jsonify({'success': True})
    return jsonify({'env': env_path.read_text() if env_path.exists() else ''})

@app.route('/bot/qr/<bot_name>')
@login_required
def get_qr(bot_name):
    return jsonify({'qr': bot_qr.get(bot_name), 'status': bot_status.get(bot_name, 'stopped')})

# ── WebSocket ─────────────────────────────────────────────

@sock.route('/ws/<bot_name>')
def bot_ws(ws, bot_name):
    if not session.get('logged_in'):
        ws.close()
        return
    qr_subscribers.setdefault(bot_name, []).append(ws)
    try:
        if bot_name in bot_qr:
            ws.send(json.dumps({'type': 'qr', 'qr': bot_qr[bot_name]}))
        ws.send(json.dumps({'type': 'status', 'status': bot_status.get(bot_name, 'stopped')}))
        for line in bot_logs.get(bot_name, [])[-50:]:
            ws.send(json.dumps({'type': 'log', 'line': line}))
    except:
        pass
    while True:
        try:
            if ws.receive(timeout=30) is None:
                break
        except:
            break
    try:
        qr_subscribers[bot_name].remove(ws)
    except:
        pass

@app.route('/status')
def status():
    return jsonify({'status': 'alive', 'bots_running': len(running_bots)})

# ── Startup: restore bots from GitHub ────────────────────
def startup():
    threading.Thread(target=restore_bots_from_github, daemon=True).start()

startup()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
