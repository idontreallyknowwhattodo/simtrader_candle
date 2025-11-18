"""
SimTrader - Improved single-file Flask app

Features (updated):
- Login for users user1..user10 with pass1..pass10 and admin/adminpass
- Admin can view users, reset portfolios/cash, and open/close market
- Simulated market (same for all users) for 15 Indian stocks
- Candlestick chart (Chart.js financial plugin) + line chart fallback
- Improved UI using Bootstrap 5
- Server-side SQLite persistence

How to run locally
1. Create virtual environment and install dependencies:
   python -m venv venv
   source venv/bin/activate   # Windows: venv\Scripts\activate
   pip install flask flask-session
2. Run the app:
   python simtrader_candle.py
3. Open http://127.0.0.1:5000

Hosting & Deployment (short guide included below in this file)

Note: This single-file app is intentionally self-contained for a small event. For production or larger classes split templates/static and use a proper WSGI server and secrets management.
"""

from flask import Flask, request, jsonify, render_template_string, redirect, url_for, session, g
from flask_session import Session
import sqlite3
import threading
import time
import random
from datetime import datetime

DB_PATH = 'simtrader.db'
MARKET_UPDATE_INTERVAL = 5.0  # seconds between price ticks
CANDLE_BUCKET = 60.0  # seconds per candlestick
PRICE_HISTORY_LENGTH = 500  # number of price points to keep
STARTING_CASH = 100000

STOCKS = [
    {"symbol": "RELIANCE", "name": "Reliance Industries"},
    {"symbol": "TCS", "name": "TCS"},
    {"symbol": "INFY", "name": "Infosys"},
    {"symbol": "HDFCBANK", "name": "HDFC Bank"},
    {"symbol": "ICICIBANK", "name": "ICICI Bank"},
    {"symbol": "HDFC", "name": "HDFC"},
    {"symbol": "LT", "name": "Larsen & Toubro"},
    {"symbol": "SBI", "name": "State Bank of India"},
    {"symbol": "KOTAKBANK", "name": "Kotak Mahindra Bank"},
    {"symbol": "AXISBANK", "name": "Axis Bank"},
    {"symbol": "ITC", "name": "ITC"},
    {"symbol": "MARUTI", "name": "Maruti Suzuki"},
    {"symbol": "BHARTIARTL", "name": "Bharti Airtel"},
    {"symbol": "SUNPHARMA", "name": "Sun Pharma"},
    {"symbol": "TATAMOTORS", "name": "Tata Motors"}
]

app = Flask(__name__)
app.secret_key = 'replace-this-with-a-random-secret'
app.config['SESSION_TYPE'] = 'filesystem'
Session(app)

# --------------------- DB helpers ---------------------

def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
    return db

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()


def init_db():
    db = get_db()
    c = db.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, username TEXT UNIQUE, password TEXT, cash REAL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS holdings (id INTEGER PRIMARY KEY, user_id INTEGER, symbol TEXT, shares INTEGER, avg_price REAL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY, user_id INTEGER, symbol TEXT, shares INTEGER, price REAL, side TEXT, ts TEXT)''')
    # price ticks (timestamped prices)
    c.execute('''CREATE TABLE IF NOT EXISTS ticks (id INTEGER PRIMARY KEY, symbol TEXT, ts REAL, price REAL)''')
    # metadata (market open/close)
    c.execute('''CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT)''')
    db.commit()

    # create users
    for i in range(1, 11):
        try:
            c.execute('INSERT INTO users (username,password,cash) VALUES (?,?,?)', (f'user{i}', f'pass{i}', STARTING_CASH))
        except sqlite3.IntegrityError:
            pass
    try:
        c.execute('INSERT INTO users (username,password,cash) VALUES (?,?,?)', ('admin', 'adminpass', STARTING_CASH))
    except sqlite3.IntegrityError:
        pass
    db.commit()

    # seed initial ticks if empty
    c.execute('SELECT COUNT(*) as cnt FROM ticks')
    cnt = c.fetchone()['cnt']
    if cnt == 0:
        now = time.time()
        for s in STOCKS:
            base = random.uniform(800, 3500)
            # create a minute of ticks to build initial candlesticks
            for i in range(120):
                ts = now - (120 - i) * (MARKET_UPDATE_INTERVAL)
                base = base * (1 + random.uniform(-0.0025, 0.0025))
                c.execute('INSERT INTO ticks (symbol,ts,price) VALUES (?,?,?)', (s['symbol'], ts, round(base,2)))
        c.execute('REPLACE INTO metadata (key,value) VALUES (?,?)', ('market_open', '1'))
        db.commit()

# --------------------- Price helpers ---------------------

def append_tick(symbol, price, ts=None):
    db = get_db()
    c = db.cursor()
    if ts is None:
        ts = time.time()
    c.execute('INSERT INTO ticks (symbol,ts,price) VALUES (?,?,?)', (symbol, ts, round(price,2)))
    # trim ticks
    c.execute('SELECT COUNT(*) as cnt FROM ticks WHERE symbol=?', (symbol,))
    cnt = c.fetchone()['cnt']
    if cnt > PRICE_HISTORY_LENGTH:
        to_delete = cnt - PRICE_HISTORY_LENGTH
        c.execute('DELETE FROM ticks WHERE id IN (SELECT id FROM ticks WHERE symbol=? ORDER BY ts ASC LIMIT ?)', (symbol, to_delete))
    db.commit()


def get_latest_price(symbol):
    db = get_db()
    c = db.cursor()
    c.execute('SELECT price FROM ticks WHERE symbol=? ORDER BY ts DESC LIMIT 1', (symbol,))
    r = c.fetchone()
    return r['price'] if r else None

# Build OHLC candles from tick data grouped by CANDLE_BUCKET
def get_candles(symbol, limit=100):
    db = get_db()
    c = db.cursor()
    cutoff = time.time() - limit * CANDLE_BUCKET
    c.execute('SELECT ts,price FROM ticks WHERE symbol=? AND ts>=? ORDER BY ts ASC', (symbol, cutoff))
    rows = c.fetchall()
    if not rows:
        return []
    candles = []
    bucket = None
    for r in rows:
        ts = r['ts']
        price = r['price']
        b = int(ts // CANDLE_BUCKET)
        if bucket is None or b != bucket:
            # start new candle
            bucket = b
            candles.append({'t': b * CANDLE_BUCKET, 'open': price, 'high': price, 'low': price, 'close': price})
        else:
            # update last candle
            cnd = candles[-1]
            cnd['high'] = max(cnd['high'], price)
            cnd['low'] = min(cnd['low'], price)
            cnd['close'] = price
    return candles

# --------------------- Market Simulator ---------------------

def market_tick():
    with app.app_context():
        db = get_db()
        c = db.cursor()
        c.execute('SELECT value FROM metadata WHERE key=?', ('market_open',))
        row = c.fetchone()
        open_flag = True
        if row and row['value'] == '0':
            open_flag = False
        if open_flag:
            for s in STOCKS:
                symbol = s['symbol']
                c.execute('SELECT price FROM ticks WHERE symbol=? ORDER BY ts DESC LIMIT 1', (symbol,))
                last = c.fetchone()['price']
                pct = random.gauss(0, 0.003)  # smaller moves for smoother candles
                if random.random() < 0.015:
                    pct += random.gauss(0, 0.015)
                newp = max(0.01, last * (1 + pct))
                append_tick(symbol, newp)
    threading.Timer(MARKET_UPDATE_INTERVAL, market_tick).start()

# --------------------- Auth ---------------------

def login_user_in_session(user_row):
    session['user_id'] = user_row['id']
    session['username'] = user_row['username']


def current_user():
    if 'user_id' not in session:
        return None
    db = get_db()
    c = db.cursor()
    c.execute('SELECT * FROM users WHERE id=?', (session['user_id'],))
    return c.fetchone()

# --------------------- Templates ---------------------

INDEX_HTML = """
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>SimTrader</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
  </head>
  <body class="bg-light">
    <div class="container py-5">
      <div class="row justify-content-center">
        <div class="col-md-6">
          <div class="card shadow-sm">
            <div class="card-body">
              <h3 class="card-title mb-3">SimTrader — Login</h3>
              <p>Login as <code>user1..user10</code> with <code>pass1..pass10</code>, or <code>admin/adminpass</code>.</p>
              <form method="post" action="/login">
                <div class="mb-3">
                  <label class="form-label">Username</label>
                  <input name="username" class="form-control" required>
                </div>
                <div class="mb-3">
                  <label class="form-label">Password</label>
                  <input type="password" name="password" class="form-control" required>
                </div>
                <div class="d-grid gap-2">
                  <button class="btn btn-primary">Login</button>
                </div>
              </form>
            </div>
          </div>
          <p class="text-muted mt-3">This app is for events and demos — passwords are intentionally simple.</p>
        </div>
      </div>
    </div>
  </body>
</html>
"""

DASH_HTML = """
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>SimTrader - Dashboard</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/chartjs-chart-financial@3.3.0/build/chartjs-chart-financial.min.js"></script>
    <style>pre{white-space:pre-wrap}</style>
  </head>
  <body class="bg-white">
    <nav class="navbar navbar-expand-lg navbar-light bg-light border-bottom">
      <div class="container-fluid">
        <a class="navbar-brand" href="#">SimTrader</a>
        <div class="d-flex">
          <span class="me-3">{{username}}</span>
          <a href="/logout" class="btn btn-outline-secondary btn-sm">Logout</a>
        </div>
      </div>
    </nav>
    <div class="container my-4">
      <div class="row">
        <div class="col-md-3">
          <div class="card mb-3">
            <div class="card-body">
              <h6>Cash</h6>
              <h4 id="cash">...</h4>
              <hr>
              <h6>Trade</h6>
              <div class="mb-2">
                <label>Symbol</label>
                <select id="symbol" class="form-select"></select>
              </div>
              <div class="mb-2">
                <label>Shares</label>
                <input id="shares" type="number" value="1" min="1" class="form-control">
              </div>
              <div class="mb-2">
                <label>Side</label>
                <select id="side" class="form-select"><option value="buy">Buy</option><option value="sell">Sell</option></select>
              </div>
              <div class="d-grid">
                <button id="trade" class="btn btn-success">Submit</button>
              </div>
              <pre id="tradeResult" class="mt-2"></pre>
            </div>
          </div>

          <div class="card">
            <div class="card-body">
              <h6>Portfolio</h6>
              <div id="portfolio">...</div>
            </div>
          </div>

          {% if is_admin %}
          <div class="card mt-3">
            <div class="card-body">
              <h6>Admin</h6>
              <div class="d-grid gap-2">
                <button id="resetAll" class="btn btn-warning">Reset all users</button>
                <button id="toggleMarket" class="btn btn-secondary">Toggle Market Open/Close</button>
              </div>
            </div>
          </div>
          {% endif %}

        </div>
        <div class="col-md-9">
          <div class="card">
            <div class="card-body">
              <div class="d-flex justify-content-between mb-2">
                <h5 id="chartTitle">Price Chart</h5>
                <div><small id="marketState">...</small></div>
              </div>
              <canvas id="candleChart" height="200"></canvas>
              <div class="mt-3">
                <h6>Recent Prices</h6>
                <pre id="prices"></pre>
              </div>
            </div>
          </div>

        </div>
      </div>
    </div>
<script>
const USER = "{{username}}";
const IS_ADMIN = {{ 'true' if is_admin else 'false' }};
let currentSymbol = '{{first_symbol}}';

async function fetchJSON(url, opts){ const r = await fetch(url, opts); if(!r.ok) { const t=await r.text(); throw new Error(t||r.statusText);} return r.json(); }

async function loadSymbols(){ const data = await fetchJSON('/api/symbols'); const sel = document.getElementById('symbol'); sel.innerHTML=''; data.forEach(s=>{ const o=document.createElement('option'); o.value=s.symbol; o.textContent=s.symbol+' - '+s.name; sel.appendChild(o); }); sel.value=currentSymbol; }

async function loadAccount(){ const data = await fetchJSON('/api/account'); document.getElementById('cash').textContent = data.cash.toFixed(2); const pdiv = document.getElementById('portfolio'); if(data.holdings.length===0) pdiv.innerHTML='<i>empty</i>'; else{ let html='<table class="table table-sm"><thead><tr><th>Symbol</th><th>Shares</th><th>Avg</th><th>Value</th></tr></thead><tbody>'; data.holdings.forEach(h=>{ html+=`<tr><td>${h.symbol}</td><td>${h.shares}</td><td>${h.avg_price.toFixed(2)}</td><td>${(h.shares*h.last_price).toFixed(2)}</td></tr>`; }); html+='</tbody></table>'; pdiv.innerHTML = html; } }

let candleChart;
async function loadCandles(symbol){ const hist = await fetchJSON('/api/candles/'+symbol); const labels = hist.map(c=> new Date(c.t*1000)); const ohlc = hist.map(c=> ({o:c.open, h:c.high, l:c.low, c:c.close, t: new Date(c.t*1000)}));
  const ctx = document.getElementById('candleChart').getContext('2d');
  if(!candleChart){
    candleChart = new Chart(ctx, {
      type: 'candlestick',
      data: { datasets: [{ label: symbol, data: ohlc }] },
      options: { animation:false, plugins: { legend:{display:false} }, scales: { x: { type: 'time', time: { unit: 'minute' } } } }
    });
  } else {
    candleChart.data.datasets[0].data = ohlc;
    candleChart.data.datasets[0].label = symbol;
    candleChart.update();
  }
  document.getElementById('prices').innerText = hist.slice(-10).map(c=> `${new Date(c.t*1000).toLocaleTimeString()} O:${c.open.toFixed(2)} H:${c.high.toFixed(2)} L:${c.low.toFixed(2)} C:${c.close.toFixed(2)}`).reverse().join('
');
}

async function doTrade(){ const symbol=document.getElementById('symbol').value; const shares=Number(document.getElementById('shares').value); const side=document.getElementById('side').value; try{ const res = await fetchJSON('/api/trade', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({symbol,shares,side})}); document.getElementById('tradeResult').textContent = JSON.stringify(res); await loadAccount(); await loadCandles(symbol); }catch(e){ document.getElementById('tradeResult').textContent = 'Error: '+e.message; } }

document.getElementById('trade').addEventListener('click', ()=> doTrade());
document.getElementById('symbol').addEventListener('change', (e)=>{ currentSymbol = e.target.value; document.getElementById('chartTitle').textContent = 'Price Chart - '+currentSymbol; loadCandles(currentSymbol); });

document.getElementById('resetAll')?.addEventListener('click', async ()=>{ if(!confirm('Reset all users?')) return; await fetch('/admin/reset',{method:'POST'}); alert('Reset done'); });

document.getElementById('toggleMarket')?.addEventListener('click', async ()=>{ const r=await fetch('/admin/toggle_market', {method:'POST'}); const t=await r.text(); alert(t); loadMeta(); });

async function loadMeta(){ const m = await fetchJSON('/api/meta'); document.getElementById('marketState').textContent = m.market_open=='1'? 'Market Open':'Market Closed'; }

async function refresh(){ try{ await loadSymbols(); await loadAccount(); await loadMeta(); await loadCandles(currentSymbol); }catch(e){ console.error(e); } }

setInterval(()=>{ loadAccount(); loadCandles(currentSymbol); loadMeta(); }, 7000);

loadSymbols().then(()=> refresh());
</script>
  </body>
</html>
"""

# --------------------- Routes ---------------------

@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return render_template_string(INDEX_HTML)

@app.route('/login', methods=['POST'])
def login():
    username = request.form.get('username')
    password = request.form.get('password')
    db = get_db(); c = db.cursor()
    c.execute('SELECT * FROM users WHERE username=? AND password=?', (username, password))
    r = c.fetchone()
    if not r:
        return 'Login failed', 401
    login_user_in_session(r)
    return redirect(url_for('dashboard'))

@app.route('/logout')
def logout():
    session.clear(); return redirect(url_for('index'))

@app.route('/dashboard')
def dashboard():
    user = current_user()
    if not user:
        return redirect(url_for('index'))
    is_admin = (user['username']=='admin')
    return render_template_string(DASH_HTML, username=user['username'], is_admin=is_admin, first_symbol=STOCKS[0]['symbol'])

# API: symbols
@app.route('/api/symbols')
def api_symbols():
    return jsonify(STOCKS)

# API: account
@app.route('/api/account')
def api_account():
    user = current_user();
    if not user: return jsonify({'error':'not logged in'}), 401
    db = get_db(); c = db.cursor()
    c.execute('SELECT cash FROM users WHERE id=?', (user['id'],))
    cash = c.fetchone()['cash']
    c.execute('SELECT symbol,shares,avg_price FROM holdings WHERE user_id=?', (user['id'],))
    holdings = []
    for r in c.fetchall():
        last = get_latest_price(r['symbol'])
        holdings.append({'symbol': r['symbol'], 'shares': r['shares'], 'avg_price': r['avg_price'] or 0.0, 'last_price': last})
    return jsonify({'cash': cash, 'holdings': holdings})

# API: candles
@app.route('/api/candles/<symbol>')
def api_candles(symbol):
    if symbol not in [s['symbol'] for s in STOCKS]:
        return jsonify([])
    candles = get_candles(symbol, limit=120)
    return jsonify(candles)

# API: trade
@app.route('/api/trade', methods=['POST'])
def api_trade():
    user = current_user();
    if not user: return jsonify({'error':'not logged in'}), 401
    data = request.get_json() or {}
    symbol = data.get('symbol')
    shares = int(data.get('shares',0))
    side = data.get('side')
    if shares <= 0: return jsonify({'error':'invalid shares count'}), 400
    if side not in ('buy','sell'): return jsonify({'error':'invalid side'}), 400
    if symbol not in [s['symbol'] for s in STOCKS]: return jsonify({'error':'unknown symbol'}), 400
    price = get_latest_price(symbol)
    if price is None: return jsonify({'error':'price not available'}), 500
    db = get_db(); c = db.cursor()
    if side == 'buy':
        cost = price * shares
        c.execute('SELECT cash FROM users WHERE id=?', (user['id'],))
        cash = c.fetchone()['cash']
        if cash < cost:
            return jsonify({'error':'insufficient cash','required':cost,'available':cash}), 400
        new_cash = cash - cost
        c.execute('UPDATE users SET cash=? WHERE id=?', (new_cash, user['id']))
        c.execute('SELECT id,shares,avg_price FROM holdings WHERE user_id=? AND symbol=?', (user['id'], symbol))
        h = c.fetchone()
        if h:
            total_shares = h['shares'] + shares
            total_cost = h['avg_price'] * h['shares'] + price * shares
            new_avg = total_cost / total_shares
            c.execute('UPDATE holdings SET shares=?, avg_price=? WHERE id=?', (total_shares, new_avg, h['id']))
        else:
            c.execute('INSERT INTO holdings (user_id,symbol,shares,avg_price) VALUES (?,?,?,?)', (user['id'], symbol, shares, price))
        c.execute('INSERT INTO trades (user_id,symbol,shares,price,side,ts) VALUES (?,?,?,?,?,?)', (user['id'], symbol, shares, price, 'buy', datetime.utcnow().isoformat()))
        db.commit()
        return jsonify({'result':'bought','symbol':symbol,'price':price,'shares':shares,'cash':new_cash})
    else:
        c.execute('SELECT id,shares,avg_price FROM holdings WHERE user_id=? AND symbol=?', (user['id'], symbol))
        h = c.fetchone()
        if not h or h['shares'] < shares:
            return jsonify({'error':'not enough shares to sell'}), 400
        proceeds = price * shares
        c.execute('SELECT cash FROM users WHERE id=?', (user['id'],))
        cash = c.fetchone()['cash']
        new_cash = cash + proceeds
        c.execute('UPDATE users SET cash=? WHERE id=?', (new_cash, user['id']))
        remaining = h['shares'] - shares
        if remaining == 0:
            c.execute('DELETE FROM holdings WHERE id=?', (h['id'],))
        else:
            c.execute('UPDATE holdings SET shares=? WHERE id=?', (remaining, h['id']))
        c.execute('INSERT INTO trades (user_id,symbol,shares,price,side,ts) VALUES (?,?,?,?,?,?)', (user['id'], symbol, shares, price, 'sell', datetime.utcnow().isoformat()))
        db.commit()
        return jsonify({'result':'sold','symbol':symbol,'price':price,'shares':shares,'cash':new_cash})

# Admin endpoints
def require_admin(user):
    return user and user['username']=='admin'

@app.route('/admin/reset', methods=['POST'])
def admin_reset():
    user = current_user();
    if not require_admin(user): return 'forbidden', 403
    db = get_db(); c = db.cursor()
    c.execute('DELETE FROM holdings')
    c.execute('DELETE FROM trades')
    c.execute('UPDATE users SET cash=?', (STARTING_CASH,))
    db.commit()
    return 'ok'

@app.route('/admin/toggle_market', methods=['POST'])
def admin_toggle():
    user = current_user();
    if not require_admin(user): return 'forbidden', 403
    db = get_db(); c = db.cursor()
    c.execute('SELECT value FROM metadata WHERE key=?', ('market_open',))
    row = c.fetchone()
    if not row or row['value']=='1':
        c.execute('REPLACE INTO metadata (key,value) VALUES (?,?)', ('market_open','0'))
        db.commit(); return 'market closed'
    else:
        c.execute('REPLACE INTO metadata (key,value) VALUES (?,?)', ('market_open','1'))
        db.commit(); return 'market opened'

@app.route('/api/meta')
def api_meta():
    db = get_db(); c = db.cursor()
    c.execute('SELECT value FROM metadata WHERE key=?', ('market_open',))
    row = c.fetchone(); v = row['value'] if row else '1'
    return jsonify({'market_open': v})

@app.route('/admin/users')
def admin_users():
    user = current_user();
    if not require_admin(user): return jsonify({'error':'forbidden'}), 403
    db = get_db(); c = db.cursor(); c.execute('SELECT id,username,cash FROM users')
    out = []
    for r in c.fetchall():
        c.execute('SELECT symbol,shares FROM holdings WHERE user_id=?', (r['id'],))
        holdings = [{'symbol':h['symbol'],'shares':h['shares']} for h in c.fetchall()]
        out.append({'username': r['username'], 'cash': r['cash'], 'holdings': holdings})
    return jsonify(out)

# --------------------- Startup ---------------------
if __name__ == '__main__':
    with app.app_context():
        init_db()
        # start market simulator
        threading.Timer(1.0, market_tick).start()
    app.run(debug=True, host='0.0.0.0')

# --------------------- Deployment notes ---------------------
"""
Deployment suggestions (short):

1) Render (recommended for ease)
   - Create a GitHub repo with this file as the app entry (e.g., simtrader_candle.py)
   - Add a requirements.txt containing: Flask, Flask-Session
   - On Render: New -> Web Service -> Connect repo -> Choose Python and set Start Command: `gunicorn simtrader_candle:app`
   - Render has a free tier suitable for hobby projects (may sleep after inactivity). See Render docs.

2) Railway (quick GitHub deploy)
   - Push repo to GitHub and create a new Railway project deploying from GitHub.
   - Railway auto-detects Python apps and uses a start command like: `gunicorn simtrader_candle:app`.

3) Deta / Deta Space (if you want always-free microservices for APIs; better for FastAPI but possible)
   - Deta Space supports Python microservices and is very easy to use for small demos.

Important: use environment variables for secrets in production. This app uses a simple secret key for demo only.

References: platform docs (Render, Railway, Deta) for up-to-date deployment steps.
"""
