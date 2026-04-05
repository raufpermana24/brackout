import ccxt
import pandas as pd
import numpy as np
import time
from datetime import datetime
import os
import requests
import concurrent.futures
import threading
import json
import websocket # Membutuhkan: pip install websocket-client
import copy      

# ==========================================
# KREDENSIAL API & TELEGRAM
# ==========================================
BINANCE_API_KEY = os.environ.get('BINANCE_API_KEY', 'fZwDMOfBL6rDU9jfUQox64fUAb2RSN48myxMPUGDAINYjmLdqJmUFhVRWLqlsX97')
BINANCE_SECRET_KEY = os.environ.get('BINANCE_API_SECRET', 'FmZNNbIOWIAddxVoLcNowLNW379E6gxyM85Bvy3QzlRMtK1eMApJp6vJtpGHWdWB')
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', '8000712659:AAHltp77nGuakOzW9QMgQpVqnd5f1KgEsKA')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '-1003812500986')

# ==========================================
# PENGATURAN BOT & VPS
# ==========================================
TIMEFRAMES = ['5m', '1h', '4h'] 
LIMIT = 100 
DATA_DIR = "vps_data" 

exchange = ccxt.binance({
    'apiKey': BINANCE_API_KEY,
    'secret': BINANCE_SECRET_KEY,
    'enableRateLimit': True,
    'options': {'defaultType': 'future'}
})

# ==========================================
# MEMORI TERKUNCI & KUMPULAN DATA
# ==========================================
SYMBOLS = []
locked_predictions = {}
last_signaled_candle = {}
performance_stats = {tf: {'benar': 0, 'salah': 0} for tf in TIMEFRAMES}
active_setups = {tf: {} for tf in TIMEFRAMES}
shared_ohlcv = {}

ccxt_to_ws_sym = {}
ws_to_ccxt_sym = {}

last_report_time = datetime.now()
stats_lock = threading.Lock()     
print_lock = threading.Lock()
data_lock = threading.Lock() 

# ==========================================
# MANAJEMEN DATA VPS (BACKUP & CLEANUP)
# ==========================================
def manage_vps_storage():
    if not os.path.exists(DATA_DIR):
        os.makedirs(DATA_DIR)

    current_time = time.time()
    for filename in os.listdir(DATA_DIR):
        filepath = os.path.join(DATA_DIR, filename)
        if os.path.isfile(filepath):
            file_age_seconds = current_time - os.path.getmtime(filepath)
            if file_age_seconds > (2 * 24 * 3600): 
                try:
                    os.remove(filepath)
                    with print_lock: print(f"🧹 VPS Auto-Clean: Menghapus data usang {filename}")
                except Exception: pass

    date_str = datetime.now().strftime("%Y-%m-%d_%H-%M")
    save_path = os.path.join(DATA_DIR, f"bot_state_{date_str}.json")

    with data_lock:
        state_to_save = {
            "performance_stats": copy.deepcopy(performance_stats),
            "locked_predictions": copy.deepcopy(locked_predictions),
            "last_signaled_candle": copy.deepcopy(last_signaled_candle),
            "shared_ohlcv": copy.deepcopy(shared_ohlcv),
            "active_setups": copy.deepcopy(active_setups)
        }

    try:
        with open(save_path, "w") as f: json.dump(state_to_save, f)
        with print_lock: print(f"💾 Data berhasil dicadangkan ke VPS: {save_path}")
    except Exception as e:
        with print_lock: print(f"⚠️ Gagal menyimpan data ke VPS: {e}")

def load_vps_data():
    global performance_stats, locked_predictions, last_signaled_candle, shared_ohlcv, active_setups
    if not os.path.exists(DATA_DIR): return False
    files = [os.path.join(DATA_DIR, f) for f in os.listdir(DATA_DIR) if f.endswith('.json')]
    if not files: return False

    latest_file = max(files, key=os.path.getctime)
    print(f"🔄 Memulihkan data memori dari VPS: {latest_file}...")
    
    try:
        with open(latest_file, "r") as f: state = json.load(f)

        with data_lock:
            if "performance_stats" in state: performance_stats.update(state["performance_stats"])
            if "active_setups" in state:
                for tf in TIMEFRAMES:
                    if tf in state["active_setups"]: active_setups[tf].update(state["active_setups"][tf])

            for sym in SYMBOLS:
                if "shared_ohlcv" in state and sym in state["shared_ohlcv"]:
                    shared_ohlcv[sym] = state["shared_ohlcv"][sym]
                if "locked_predictions" in state and sym in state["locked_predictions"]:
                    locked_predictions[sym] = state["locked_predictions"][sym]
                if "last_signaled_candle" in state and sym in state["last_signaled_candle"]:
                    last_signaled_candle[sym] = state["last_signaled_candle"][sym]
                    
        print("✅ Pemulihan VPS selesai! Bot tidak perlu mengulang dari nol.")
        return True
    except Exception as e:
        print(f"⚠️ Gagal memulihkan data: {e}")
        return False

def get_all_usdt_futures():
    print("⏳ Mengambil daftar semua market USDT Futures aktif...")
    try:
        markets = exchange.load_markets()
        symbols = [sym for sym, m in markets.items() if m.get('active') and m.get('quote') == 'USDT' and m.get('contract')]
        print(f"✅ Berhasil menemukan {len(symbols)} market USDT Futures.")
        return symbols
    except Exception as e:
        print(f"⚠️ Gagal mengambil daftar market: {e}")
        return ['BTC/USDT:USDT']

def initialize_memory():
    global SYMBOLS, locked_predictions, last_signaled_candle, shared_ohlcv, active_setups
    SYMBOLS = get_all_usdt_futures()
    
    locked_predictions = {sym: {tf: None for tf in TIMEFRAMES} for sym in SYMBOLS}
    last_signaled_candle = {sym: {tf: None for tf in TIMEFRAMES} for sym in SYMBOLS}
    active_setups = {tf: {} for tf in TIMEFRAMES}
    shared_ohlcv = {sym: {tf: [] for tf in TIMEFRAMES} for sym in SYMBOLS}
    
    for sym in SYMBOLS:
        ws_sym = sym.split(':')[0].replace('/', '').lower()
        ccxt_to_ws_sym[sym] = ws_sym
        ws_to_ccxt_sym[ws_sym] = sym
        
    return load_vps_data()

# ==========================================
# ENGINE WEBSOCKET HYBRID
# ==========================================
def ws_on_message(ws, message):
    try:
        data = json.loads(message)
        if 'data' in data and 'k' in data['data']:
            kline = data['data']['k']
            sym_ws = kline['s'].lower()
            tf = kline['i']
            
            if sym_ws in ws_to_ccxt_sym and tf in TIMEFRAMES:
                sym_ccxt = ws_to_ccxt_sym[sym_ws]
                candle = [kline['t'], float(kline['o']), float(kline['h']), float(kline['l']), float(kline['c']), float(kline['v'])]
                with data_lock:
                    history = shared_ohlcv[sym_ccxt][tf]
                    if not history: history.append(candle)
                    else:
                        last_t = history[-1][0]
                        if candle[0] == last_t: history[-1] = candle 
                        elif candle[0] > last_t:
                            history.append(candle)
                            if len(history) > LIMIT: history.pop(0)
    except Exception: pass

def ws_on_open(ws):
    print("📡 Terhubung ke Binance WebSocket! Berlangganan Stream Data...")
    streams = [f"{ccxt_to_ws_sym[sym]}@kline_{tf}" for sym in SYMBOLS for tf in TIMEFRAMES]
    for i in range(0, len(streams), 50):
        chunk = streams[i:i+50]
        ws.send(json.dumps({"method": "SUBSCRIBE", "params": chunk, "id": i}))
        time.sleep(0.3)
    print("✅ Berhasil Berlangganan Live Data WebSocket!")

def start_websocket_thread():
    url = "wss://fstream.binance.com/stream"
    ws = websocket.WebSocketApp(url, on_message=ws_on_message, on_open=ws_on_open)
    threading.Thread(target=lambda: ws.run_forever(), daemon=True).start()

# ==========================================
# FUNGSI TELEGRAM (ON-DEMAND POLLING DENGAN SUMMARY MENU)
# ==========================================
def send_telegram_message(message, target_chat_id=None):
    chat = target_chat_id if target_chat_id else TELEGRAM_CHAT_ID
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": chat, "text": message, "parse_mode": "HTML"}
    try: requests.post(url, json=payload, timeout=10)
    except Exception as e: 
        with print_lock: print(f"⚠️ Gagal kirim ke Telegram: {e}")

def get_summary_message():
    """Menghasilkan teks laporan jumlah koin per timeframe"""
    msg = "<b>📢 HASIL PEMINDAIAN PASAR SAAT INI</b>\n\n"
    msg += "Bot telah mengumpulkan data terbaru. Berikut jumlah koin yang memiliki setup aktif:\n\n"
    
    total_signals = 0
    for tf in TIMEFRAMES:
        count = len(active_setups.get(tf, {}))
        total_signals += count
        msg += f"🕒 Timeframe <b>{tf}</b> : Terdapat <b>{count} Sinyal</b>\n"
    
    msg += "\n<i>👉 Silakan balas/ketik timeframe yang ingin Anda lihat (contoh: <b>/5m</b>, <b>/1h</b>, atau <b>/4h</b>)</i>\n"
    msg += "<i>👉 Ketik <b>/status</b> kapan saja untuk melihat menu ini lagi.</i>"
    
    if total_signals == 0:
        msg += "\n\n⚠️ <i>Pasar sedang konsolidasi, belum ada setup valid ditemukan.</i>"
        
    return msg

def format_setup_table(tf_request):
    setups = active_setups.get(tf_request, {})
    if not setups:
        return f"<b>📊 DATA SETUP AKTIF: {tf_request}</b>\n\n<i>Kondisi Market: Sedang Konsolidasi.\nBelum ada setup yang valid saat ini.</i>"

    text = f"<b>📊 DATA SETUP AKTIF: {tf_request}</b>\n<pre>\n"
    text += f"{'KOIN':<8} | {'DIR':<4} | {'PAUS':<4} | {'SETUP'}\n"
    text += "-" * 34 + "\n"

    # Urutkan berdasarkan Abjad Koin agar rapi
    sorted_syms = sorted(setups.keys())
    
    for sym in sorted_syms:
        data = setups[sym]
        clean_sym = sym.split('/')[0] 
        if len(clean_sym) > 8: clean_sym = clean_sym[:8]

        dir_str = "LONG" if data['dir'] == "UP" else "SHRT"
        whale_str = data['whale']

        s_name = data['setup_name'].split('(')[0].strip() 
        s_name = s_name.replace('ASCENDING TRIANGLE BREAKOUT', 'TRIANGLE')
        s_name = s_name.replace('DESCENDING TRIANGLE BREAKDOWN', 'TRIANGLE')
        s_name = s_name.replace('SYMMETRICAL TRIANGLE BREAKOUT UP', 'TRIANGLE')
        s_name = s_name.replace('SYMMETRICAL TRIANGLE BREAKDOWN DOWN', 'TRIANGLE')
        s_name = s_name.replace('PERFECT', 'PRFCT')
        s_name = s_name.replace('EXTREME', 'EXTRM')
        s_name = s_name.replace('STOCH', 'STOCH')
        s_name = s_name.replace('MA8/MA13 GOLDEN CROSS', 'MA8/13 UP')
        s_name = s_name.replace('MA8/MA13 DEATH CROSS', 'MA8/13 DN')
        s_name = s_name.replace('MA5 CROSSOVER', 'MA5')
        if len(s_name) > 10: s_name = s_name[:10]

        text += f"{clean_sym:<8} | {dir_str:<4} |  {whale_str}  | {s_name}\n"

    text += "</pre>\n<i>Keterangan PAUS:\n🟢 = Dominan Beli | 🔴 = Dominan Jual</i>"
    return text

def telegram_polling_thread():
    offset = None
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    print("📡 Bot Pendengar Telegram Aktif. Menunggu perintah user di Grup/Channel...")
    
    while True:
        try:
            params = {'timeout': 20}
            if offset: params['offset'] = offset
            response = requests.get(url, params=params, timeout=25).json()

            if 'result' in response:
                for update in response['result']:
                    offset = update['update_id'] + 1
                    
                    msg_data = None
                    if 'message' in update: msg_data = update['message']
                    elif 'channel_post' in update: msg_data = update['channel_post']
                    
                    if msg_data and 'text' in msg_data:
                        chat_id = msg_data['chat']['id']
                        text = msg_data['text'].strip().lower()

                        chat_type = msg_data['chat'].get('type', 'unknown')
                        with print_lock:
                            print(f"\n📩 Pesan {chat_type} Diterima: '{text}' (Chat ID: {chat_id})")

                        # Cek Command Menu (Summary)
                        if 'status' in text or 'menu' in text or 'scan' in text:
                            with print_lock: print(f"✅ Mengirim Ringkasan (Status) ke Chat {chat_id}...")
                            reply_msg = get_summary_message()
                            send_telegram_message(reply_msg, target_chat_id=chat_id)
                            continue

                        # Cek Command Tabel Timeframe
                        tf_cmd = text.replace('/', '')
                        if tf_cmd in TIMEFRAMES:
                            with print_lock: print(f"✅ Mengeksekusi permintaan tabel {tf_cmd} ke Chat {chat_id}...")
                            reply_msg = format_setup_table(tf_cmd)
                            send_telegram_message(reply_msg, target_chat_id=chat_id)
                        else:
                            with print_lock: print(f"ℹ️ Pesan diabaikan (Bukan perintah valid).")
                            
        except requests.exceptions.ReadTimeout:
            pass 
        except Exception as e:
            with print_lock: print(f"⚠️ Telegram Polling Error: {e}")
            time.sleep(2)

def send_hourly_report():
    global last_report_time
    table_str = "<b>📊 REKAP AKURASI BOT (1 JAM) 📊</b>\n<pre>\n"
    table_str += f"{'TF':<4} | {'BNR':<4} | {'SLH':<4} | {'AKURASI':<7}\n"
    table_str += "-" * 33 + "\n"
    total_benar = total_salah = 0
    with stats_lock:
        for tf in TIMEFRAMES:
            benar = performance_stats[tf]['benar']
            salah = performance_stats[tf]['salah']
            total = benar + salah
            akurasi = (benar / total * 100) if total > 0 else 0.0
            table_str += f"{tf:<4} | {benar:<4} | {salah:<4} | {akurasi:>6.1f}%\n"
            total_benar += benar; total_salah += salah
            performance_stats[tf]['benar'] = performance_stats[tf]['salah'] = 0
        last_report_time = datetime.now()

    table_str += "-" * 33 + "\n"
    total_akurasi = (total_benar / (total_benar + total_salah) * 100) if (total_benar + total_salah) > 0 else 0.0
    table_str += f"{'ALL':<4} | {total_benar:<4} | {total_salah:<4} | {total_akurasi:>6.1f}%\n</pre>\n"
    send_telegram_message(table_str)
    
    clean_terminal = table_str.replace("<b>", "").replace("</b>", "").replace("<pre>\n", "").replace("</pre>\n", "")
    with print_lock: print(f"\n======================================================\n{clean_terminal}\n======================================================\n")

# ==========================================
# INDIKATOR, WHALE TRACKER & LOGIKA
# ==========================================
def get_order_book_whale_status(symbol):
    try:
        ob = exchange.fetch_order_book(symbol, limit=20)
        bids, asks = ob['bids'], ob['asks']
        if not bids or not asks: return "⚪"
        total_bid = sum(b[1] for b in bids)
        total_ask = sum(a[1] for a in asks)
        tot = total_bid + total_ask
        if tot == 0: return "⚪"
        
        buy_pct = (total_bid / tot) * 100
        if buy_pct > 60: return "🟢" 
        if buy_pct < 40: return "🔴" 
        return "⚪"
    except: return "⚪"

def calculate_all_indicators(df):
    df['MID_BB'] = df['close'].rolling(window=20).mean()
    df['STD_20'] = df['close'].rolling(window=20).std()
    df['TOP_BB'] = df['MID_BB'] + (df['STD_20'] * 2)
    df['LOW_BB'] = df['MID_BB'] - (df['STD_20'] * 2)
    df['MA5_HIGH'] = df['high'].rolling(window=5).mean()
    df['MA5_LOW'] = df['low'].rolling(window=5).mean()
    df['MA5_CLOSE'] = df['close'].rolling(window=5).mean()
    df['MA8_CLOSE'] = df['close'].rolling(window=8).mean()   
    df['MA13_CLOSE'] = df['close'].rolling(window=13).mean() 
    df['AVG_VOL'] = df['volume'].rolling(window=20).mean()

    low_min = df['low'].rolling(window=5).min()
    high_max = df['high'].rolling(window=5).max()
    fast_k = 100 * ((df['close'] - low_min) / (high_max - low_min).replace(0, 0.0001))
    df['STOCH_K'] = fast_k.rolling(window=3).mean()
    df['STOCH_D'] = df['STOCH_K'].rolling(window=3).mean()
    return df

def detect_triangle_pattern(df, window=15):
    if len(df) < window + 2: return False, "", "NETRAL"
    curr, prev = df.iloc[-1], df.iloc[-2]
    highs, lows = df['high'].iloc[-window-1:-1].values, df['low'].iloc[-window-1:-1].values
    x = np.arange(window)
    slope_high, _ = np.polyfit(x, highs, 1)
    slope_low, _ = np.polyfit(x, lows, 1)
    
    pct_slope_high, pct_slope_low = (slope_high / curr['close']) * 100, (slope_low / curr['close']) * 100
    is_high_flat, is_low_flat = abs(pct_slope_high) < 0.05, abs(pct_slope_low) < 0.05
    is_high_falling, is_low_rising = pct_slope_high < -0.06, pct_slope_low > 0.06
    max_high_pola, min_low_pola = np.max(highs), np.min(lows)
    
    breakout_up = curr['close'] > max_high_pola and prev['close'] <= max_high_pola
    breakdown_down = curr['close'] < min_low_pola and prev['close'] >= min_low_pola

    if is_high_flat and is_low_rising and breakout_up: return True, "ASCENDING TRIANGLE BREAKOUT 🚀🔼", "UP"
    elif is_low_flat and is_high_falling and breakdown_down: return True, "DESCENDING TRIANGLE BREAKDOWN ☄️🔽", "DOWN"
    elif is_high_falling and is_low_rising:
        if breakout_up: return True, "SYMMETRICAL TRIANGLE BREAKOUT UP 🚀🔼", "UP"
        elif breakdown_down: return True, "SYMMETRICAL TRIANGLE BREAKDOWN DOWN ☄️🔽", "DOWN"
    return False, "", "NETRAL"

def detect_trading_setup(df):
    curr, prev = df.iloc[-1], df.iloc[-2]
    setup_name, direction = "MENCARI SETUP ⚪", "NETRAL"

    is_triangle, tri_setup_name, tri_dir = detect_triangle_pattern(df)
    if is_triangle: return tri_setup_name, tri_dir

    bbma_sell = curr['MA5_HIGH'] > curr['TOP_BB'] and curr['close'] <= curr['TOP_BB'] and curr['close'] < curr['open']
    bbma_buy = curr['MA5_LOW'] < curr['LOW_BB'] and curr['close'] >= curr['LOW_BB'] and curr['close'] > curr['open']

    stoch_sell = (prev['STOCH_K'] > prev['STOCH_D']) and (curr['STOCH_K'] < curr['STOCH_D']) and (curr['STOCH_K'] > 80)
    stoch_buy = (prev['STOCH_K'] < prev['STOCH_D']) and (curr['STOCH_K'] > curr['STOCH_D']) and (curr['STOCH_K'] < 20)

    ma813_cross_up = (prev['MA8_CLOSE'] <= prev['MA13_CLOSE']) and (curr['MA8_CLOSE'] > curr['MA13_CLOSE']) and (curr['close'] > curr['MA8_CLOSE'])
    ma813_cross_down = (prev['MA8_CLOSE'] >= prev['MA13_CLOSE']) and (curr['MA8_CLOSE'] < curr['MA13_CLOSE']) and (curr['close'] < curr['MA8_CLOSE'])

    ma5_cross_up = (prev['close'] <= prev['MA5_CLOSE']) and (curr['close'] > curr['MA5_CLOSE'])
    ma5_cross_down = (prev['close'] >= prev['MA5_CLOSE']) and (curr['close'] < curr['MA5_CLOSE'])

    if bbma_sell and stoch_sell: return "PERFECT SELL (Extreme BBMA + Stoch Cross >80) 🌟🔴", "DOWN"
    elif bbma_buy and stoch_buy: return "PERFECT BUY (Extreme BBMA + Stoch Cross <20) 🌟🟢", "UP"
    elif bbma_sell: return "EXTREME SELL (MA5 Keluar Top BB) ⚠️🔴", "DOWN"
    elif bbma_buy: return "EXTREME BUY (MA5 Keluar Low BB) ⚠️🟢", "UP"
    elif stoch_sell: return "STOCH SELL (Garis %K Memotong %D ke Bawah di >80) 📉🔴", "DOWN"
    elif stoch_buy: return "STOCH BUY (Garis %K Memotong %D ke Atas di <20) 📈🟢", "UP"
    elif ma813_cross_up: return "MA8/MA13 GOLDEN CROSS (Candle Close Konfirmasi Naik) ⚔️📈", "UP"
    elif ma813_cross_down: return "MA8/MA13 DEATH CROSS (Candle Close Konfirmasi Turun) ⚔️📉", "DOWN"
    elif ma5_cross_up: return "MA5 CROSSOVER BUY (Candle Menembus & Close di atas MA5) 📈🟢", "UP"
    elif ma5_cross_down: return "MA5 CROSSOVER SELL (Candle Menembus & Close di bawah MA5) 📉🔴", "DOWN"

    return setup_name, direction

def evaluate_past_prediction(symbol, tf, closed_candle, past_prediction):
    actual_dir = "UP 🟢" if closed_candle['close'] > closed_candle['open'] else "DOWN 🔴"
    pred_dir = past_prediction['pred_dir']
    
    with stats_lock:
        if (actual_dir == "UP 🟢" and pred_dir == "UP") or (actual_dir == "DOWN 🔴" and pred_dir == "DOWN"):
            performance_stats[tf]['benar'] += 1
        else:
            performance_stats[tf]['salah'] += 1

def process_data(symbol, tf):
    try:
        with data_lock:
            ohlcv = shared_ohlcv[symbol][tf].copy()
            
        if len(ohlcv) < 30:
            try:
                ohlcv = exchange.fetch_ohlcv(symbol, timeframe=tf, limit=LIMIT)
                with data_lock: shared_ohlcv[symbol][tf] = ohlcv 
            except Exception: return 
                
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df = calculate_all_indicators(df)
        df = df.dropna().reset_index(drop=True)
        if len(df) < 5: return
        
        current_candle = df.iloc[-1]
        closed_candle = df.iloc[-2]
        
        setup_name, pred_dir = detect_trading_setup(df)
        
        past_pred = locked_predictions[symbol][tf]
        if past_pred is not None and past_pred['timestamp'] == closed_candle['timestamp']:
            evaluate_past_prediction(symbol, tf, closed_candle, past_pred)
            
        if pred_dir != "NETRAL":
            whale_status = get_order_book_whale_status(symbol)
            active_setups[tf][symbol] = {'dir': pred_dir, 'setup_name': setup_name, 'whale': whale_status}
            locked_predictions[symbol][tf] = {'timestamp': current_candle['timestamp'], 'pred_dir': pred_dir, 'setup_name': setup_name}
        else:
            if symbol in active_setups[tf]: del active_setups[tf][symbol]
            locked_predictions[symbol][tf] = None

    except Exception: pass

# ==========================================
# [BARU] FUNGSI PEMINDAIAN MASSAL AWAL (MASS SCAN)
# ==========================================
def initial_mass_scan():
    """Memindai seluruh 200+ koin pada saat bot dinyalakan sebelum masuk ke mode background"""
    print("\n🔍 Memulai Pemindaian Massal (Mass Scan) seluruh koin. Proses ini memakan waktu 1-2 Menit...")
    send_telegram_message("⏳ <b>Menyiapkan Database Market...</b>\nBot sedang memindai seluruh 200+ koin Futures. Mohon tunggu sekitar 1-2 menit...")
    
    # Gunakan ThreadPool untuk mempercepat download REST API secara paralel
    with concurrent.futures.ThreadPoolExecutor(max_workers=15) as executor:
        futures = [executor.submit(process_data, sym, tf) for sym in SYMBOLS for tf in TIMEFRAMES]
        # Tampilkan progress bar sederhana di terminal
        total = len(futures)
        for i, future in enumerate(concurrent.futures.as_completed(futures)):
            if i % 50 == 0:
                print(f"   [Progress: {i}/{total} Tugas Selesai...]")
                
    print("✅ Mass Scan Selesai! Mengirim Laporan Ringkasan ke Telegram...")
    
    # Setelah scan selesai, kirim laporan summary
    summary_msg = get_summary_message()
    send_telegram_message(summary_msg)

# ==========================================
# BACKTEST 
# ==========================================
def run_backtest():
    is_restored = initialize_memory()
    print("\n⏳ MENGAMBIL DATA 1 MINGGU KE BELAKANG UNTUK BACKTEST...")
    send_telegram_message("⏳ <b>Proses Backtest Dimulai...</b>\nMensimulasikan Top 10 Koin Futures selama 1 minggu terakhir.")
    
    sample_symbols = SYMBOLS[:10]
    days = 7
    now_ms = exchange.milliseconds()
    start_ms = now_ms - (days * 24 * 60 * 60 * 1000)
    results = {tf: {'benar': 0, 'salah': 0, 'total': 0} for tf in TIMEFRAMES}

    for sym in sample_symbols:
        clean_sym = sym.replace(':USDT', '')
        print(f"🔄 Menganalisis Market {clean_sym}...")
        for tf in TIMEFRAMES:
            all_ohlcv = []
            since = start_ms
            
            while since < now_ms:
                try:
                    ohlcv = exchange.fetch_ohlcv(sym, timeframe=tf, since=since, limit=1000)
                    if not ohlcv: break
                    all_ohlcv.extend(ohlcv)
                    since = ohlcv[-1][0] + 1
                    time.sleep(0.05)
                except Exception: break
                    
            if not all_ohlcv: continue
            
            df = pd.DataFrame(all_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df = calculate_all_indicators(df)
            df = df.dropna().reset_index(drop=True)

            for i in range(20, len(df)):
                df_subset = df.iloc[:i+1]
                setup_name, pred_dir = detect_trading_setup(df_subset)

                if pred_dir != "NETRAL" and i + 1 < len(df):
                    actual_dir = "UP" if df.iloc[i+1]['close'] > df.iloc[i+1]['open'] else "DOWN"
                    results[tf]['total'] += 1
                    if pred_dir == actual_dir: results[tf]['benar'] += 1
                    else: results[tf]['salah'] += 1

    telegram_msg = "<b>📊 HASIL BACKTEST TOP 10 FUTURES 📊</b>\n<pre>\n"
    telegram_msg += f"{'TF':<4} | {'TOT':<4} | {'BNR':<4} | {'SLH':<4} | {'AKURASI':<7}\n"
    telegram_msg += "-" * 33 + "\n"
    
    print("\n=======================================================================")
    print("📊 HASIL BACKTEST GABUNGAN (TOP 10 KOIN FUTURES)")
    print("=======================================================================")
    print(f"{'TIMEFRAME':<10} | {'TOTAL SETUP':<13} | {'BENAR':<8} | {'SALAH':<8} | {'AKURASI':<8}")
    print("-" * 69)
    
    total_semua = benar_semua = salah_semua = 0

    for tf in TIMEFRAMES:
        res = results[tf]
        tot, bnr, slh = res['total'], res['benar'], res['salah']
        akurasi = (bnr / tot * 100) if tot > 0 else 0
        total_semua += tot; benar_semua += bnr; salah_semua += slh
        telegram_msg += f"{tf:<4} | {tot:<4} | {bnr:<4} | {slh:<4} | {akurasi:>6.1f}%\n"
        print(f"{tf:<10} | {tot:<13} | {bnr:<8} | {slh:<8} | {akurasi:.2f}%")
        
    akurasi_total = (benar_semua / total_semua * 100) if total_semua > 0 else 0
    telegram_msg += "-" * 33 + "\n"
    telegram_msg += f"{'ALL':<4} | {total_semua:<4} | {benar_semua:<4} | {salah_semua:<4} | {akurasi_total:>6.1f}%\n</pre>"
    send_telegram_message(telegram_msg)
    input("Tekan [ENTER] untuk kembali ke Menu Utama...")

def run_bot():
    is_restored = initialize_memory()
    print("\n======================================================")
    print("🚀 MENJALANKAN BOT VPS DAEMON (AUTO-SCAN & BACKGROUND)")
    print("======================================================\n")

    # Jalankan Pemindaian Massal dulu (Hanya jika bot dinyalakan ulang dari nol)
    if not is_restored:
        initial_mass_scan()
    else:
        send_telegram_message("🤖 <b>Bot Berhasil Direstart!</b>\nData dari VPS sukses dipulihkan. Ketik <b>/status</b> untuk melihat ringkasan pasar.")

    # Mulai menyedot data live tanpa henti
    start_websocket_thread()
    # Mulai menunggu perintah chat dari Telegram
    threading.Thread(target=telegram_polling_thread, daemon=True).start()

    global last_report_time
    last_report_time = datetime.now() 

    try:
        while True:
            # Perbarui kondisi setup semua koin setiap 5 detik di background
            with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
                futures = [executor.submit(process_data, sym, tf) for sym in SYMBOLS for tf in TIMEFRAMES]
                concurrent.futures.wait(futures)
            
            if (datetime.now() - last_report_time).total_seconds() >= 3600:
                send_hourly_report()
                manage_vps_storage()
                        
            time.sleep(5) 
            
    except KeyboardInterrupt:
        print("\n\n🛑 Bot dihentikan.")
        print("💾 Melakukan backup darurat sebelum keluar...")
        manage_vps_storage()
        send_telegram_message("🛑 <b>Bot dihentikan. Data telah dibackup dengan aman ke VPS.</b>")
        time.sleep(1) 

def main_menu():
    while True:
        os.system('cls' if os.name == 'nt' else 'clear') 
        print("======================================================")
        print("🌟 MENU UTAMA BOT VPS (MASS SCAN & ON-DEMAND) 🌟")
        print("======================================================")
        print("1. 🚀 Jalankan Auto-Scan & Pengumpul Background")
        print("2. 📊 Analisis Backtest (Sampel 10 Koin)")
        print("3. ❌ Keluar / Matikan Program")
        print("======================================================")
        
        pilihan = input("👉 Pilih menu (1/2/3): ")
        if pilihan == '1': run_bot()
        elif pilihan == '2': run_backtest()
        elif pilihan == '3': break
        else: time.sleep(2)

if __name__ == "__main__":
    main_menu()
