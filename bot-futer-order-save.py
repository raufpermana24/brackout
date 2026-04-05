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
import traceback 

import matplotlib
matplotlib.use('Agg') 
import matplotlib.pyplot as plt
import io

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
# MANAJEMEN DATA VPS
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
    except Exception as e: pass

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
                if "shared_ohlcv" in state and sym in state["shared_ohlcv"]: shared_ohlcv[sym] = state["shared_ohlcv"][sym]
                if "locked_predictions" in state and sym in state["locked_predictions"]: locked_predictions[sym] = state["locked_predictions"][sym]
                if "last_signaled_candle" in state and sym in state["last_signaled_candle"]: last_signaled_candle[sym] = state["last_signaled_candle"][sym]
                    
        print("✅ Pemulihan VPS selesai!")
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
# FUNGSI TELEGRAM & GRAFIK
# ==========================================
def send_telegram_message(message, target_chat_id=None):
    chat = target_chat_id if target_chat_id else TELEGRAM_CHAT_ID
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": chat, "text": message, "parse_mode": "HTML"}
    try: requests.post(url, json=payload, timeout=10)
    except Exception: pass

def send_telegram_photo(caption, photo_buf, target_chat_id=None):
    chat = target_chat_id if target_chat_id else TELEGRAM_CHAT_ID
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    files = {'photo': ('chart.png', photo_buf, 'image/png')}
    data = {'chat_id': chat, 'caption': caption, 'parse_mode': 'HTML'}
    try: requests.post(url, files=files, data=data, timeout=15)
    except Exception: pass

def generate_candlestick_chart(symbol, tf, df):
    df_chart = df.tail(50).reset_index(drop=True)
    fig, ax = plt.subplots(figsize=(8, 4), facecolor='#1e1e2f')
    ax.set_facecolor('#1e1e2f')

    up = df_chart[df_chart['close'] >= df_chart['open']]
    down = df_chart[df_chart['close'] < df_chart['open']]
    width, width2 = 0.6, 0.05

    ax.bar(up.index, up['close'] - up['open'], width, bottom=up['open'], color='#00ff88', edgecolor='none')
    ax.bar(up.index, up['high'] - up['low'], width2, bottom=up['low'], color='#00ff88', edgecolor='none')
    ax.bar(down.index, down['close'] - down['open'], width, bottom=down['open'], color='#ff3366', edgecolor='none')
    ax.bar(down.index, down['high'] - down['low'], width2, bottom=down['low'], color='#ff3366', edgecolor='none')

    ax.plot(df_chart.index, df_chart['MA8_CLOSE'], color='#00d4ff', linewidth=1.5, label='MA 8')
    ax.plot(df_chart.index, df_chart['MA13_CLOSE'], color='#ffaa00', linewidth=1.5, label='MA 13')

    clean_sym = symbol.replace(':USDT', '')
    ax.set_title(f"Grafik {clean_sym} ({tf}) - VIP Setup", color='white', fontsize=12)
    ax.tick_params(colors='white')
    ax.grid(color='#2a2a3f', linestyle='--', linewidth=0.5)
    ax.legend(loc='upper left', facecolor='#1e1e2f', labelcolor='white', edgecolor='none')
    plt.tight_layout()
    
    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=100, facecolor=fig.get_facecolor(), edgecolor='none')
    buf.seek(0)
    plt.close(fig)
    return buf

def get_summary_message():
    msg = "<b>📢 HASIL PEMINDAIAN PASAR SAAT INI</b>\n\n"
    msg += "Bot telah mengumpulkan data teknikal. (Sinyal akan difilter secara ketat saat Anda memintanya):\n\n"
    
    for tf in TIMEFRAMES:
        count = len(active_setups.get(tf, {}))
        msg += f"🕒 Timeframe <b>{tf}</b> : <b>{count} Koin Berpotensi</b>\n"
    
    msg += "\n<i>👉 Ketik timeframe yang ingin dianalisis (contoh: <b>/5m</b>, <b>/1h</b>)</i>\n"
    msg += "<i>Bot hanya akan mengirimkan sinyal VIP yang dikonfirmasi oleh Open Interest (OI) / Paus.</i>"
    return msg

def get_open_interest_analysis(symbol, tf, current_price, prev_price):
    try:
        clean_sym = symbol.split(':')[0].replace('/', '')
        url = f"https://fapi.binance.com/futures/data/openInterestHist?symbol={clean_sym}&period={tf}&limit=2"
        res = requests.get(url, timeout=5).json()
        
        if len(res) >= 2:
            oi_prev = float(res[0]['sumOpenInterestValue'])
            oi_curr = float(res[1]['sumOpenInterestValue'])
            oi_up = oi_curr > oi_prev
            price_up = current_price > prev_price
            
            if oi_up and price_up: return "🟢 Bullish Menguat (OI ⬆️ Harga ⬆️)", 2 # Skor 2 = Sangat Kuat LONG
            elif oi_up and not price_up: return "🔴 Bearish Menguat (OI ⬆️ Harga ⬇️)", -2 # Skor -2 = Sangat Kuat SHORT
            elif not oi_up and price_up: return "⚠️ Bullish Melemah (OI ⬇️ Harga ⬆️)", -1 # Fakeout
            elif not oi_up and not price_up: return "⚠️ Bearish Melemah (OI ⬇️ Harga ⬇️)", 1 # Fakeout
    except Exception: pass
    return "⚪ Data OI Tidak Tersedia", 0

def send_detailed_setups(chat_id, tf_request):
    setups = active_setups.get(tf_request, {})
    
    if not setups:
        empty_msg = f"<b>📊 ANALISIS KOIN AKTIF ({tf_request})</b>\n\n<i>Kondisi Market: Sedang Konsolidasi.\nBelum ada koin dengan setup valid.</i>"
        send_telegram_message(empty_msg, target_chat_id=chat_id)
        return

    send_telegram_message(f"⏳ <b>Memproses {len(setups)} sinyal mentah...</b>\nMenerapkan Smart Filter (Mencari konfirmasi Paus & Modal Baru OI). Proses ini memakan waktu beberapa detik...", chat_id)

    sorted_syms = sorted(setups.keys())
    vip_signals_to_send = []

    # ==========================================
    # LOGIKA SMART FILTER (SINYAL PENTING)
    # ==========================================
    for sym in sorted_syms:
        data = setups[sym]
        
        # 1. Tarik Data OI dan Whale terlebih dahulu
        try:
            ob = exchange.fetch_order_book(sym, limit=20)
            bids, asks = ob['bids'], ob['asks']
            total_bid = sum(b[1] for b in bids)
            total_ask = sum(a[1] for a in asks)
            tot = total_bid + total_ask
            buy_pct = (total_bid / tot) * 100 if tot > 0 else 50
            if buy_pct > 55: whale_stat, whale_score = f"🟢 Dominan Beli ({buy_pct:.0f}%)", 1
            elif buy_pct < 45: whale_stat, whale_score = f"🔴 Dominan Jual ({(100-buy_pct):.0f}%)", -1
            else: whale_stat, whale_score = f"⚪ Netral ({buy_pct:.0f}% Beli)", 0
        except:
            whale_stat, whale_score = "⚪ Data Paus Tidak Tersedia", 0

        oi_stat, oi_score = get_open_interest_analysis(sym, tf_request, data['price'], data['prev_price'])

        # 2. Proses Filtrasi Sinyal (Penyaringan Sinyal Ampas)
        is_important = False
        dir_long = data['dir'] == "UP"
        setup_name = data['setup_name']
        
        # Apakah ini setup papan atas? (Sangat Akurat)
        is_super_setup = "PERFECT" in setup_name or "EXTREME" in setup_name or "TRIANGLE" in setup_name
        
        # Kondisi Filter untuk posisi LONG
        if dir_long:
            if is_super_setup and oi_score >= 0: 
                is_important = True # Setup langka, dan OI tidak melemah
            elif oi_score == 2: 
                is_important = True # Teknikal biasa, tapi Bandar injek modal besar-besaran (Valid)
            elif whale_score == 1 and ("CROSS" in setup_name or "STOCH" in setup_name) and oi_score >= 0:
                is_important = True # Persilangan didukung tembok beli Paus (Valid)
        # Kondisi Filter untuk posisi SHORT
        else:
            if is_super_setup and oi_score <= 0:
                is_important = True 
            elif oi_score == -2:
                is_important = True 
            elif whale_score == -1 and ("CROSS" in setup_name or "STOCH" in setup_name) and oi_score <= 0:
                is_important = True 

        # Jika Lolos Filter, masukkan ke daftar kirim Telegram
        if is_important:
            clean_sym = sym.split('/')[0] 
            dir_icon = "🟢 LONG" if dir_long else "🔴 SHORT"
            
            caption = f"<b>⭐ VIP SETUP: {clean_sym}/USDT | {dir_icon}</b>\n"
            caption += f"📝 <b>Sinyal:</b> {setup_name}\n"
            caption += f"🐋 <b>Order Book:</b> {whale_stat}\n"
            caption += f"📊 <b>Open Interest:</b> {oi_stat}\n"
            caption += f"📈 <b>Harga:</b> ${data['price']:.4f}\n"
            caption += f"⚡ <b>Stoch K/D:</b> {data['stoch_k']:.1f} / {data['stoch_d']:.1f}\n"
            caption += f"📏 <b>MA5/8/13:</b> ${data['ma5']:.4f} | ${data['ma8']:.4f} | ${data['ma13']:.4f}"
            
            vip_signals_to_send.append({'sym': sym, 'caption': caption})

    # ==========================================
    # PENGIRIMAN HASIL FILTRASI
    # ==========================================
    if len(vip_signals_to_send) == 0:
        send_telegram_message(f"🗑️ <b>Filter Selesai!</b>\nDari {len(setups)} sinyal teknikal awal di {tf_request}, <b>TIDAK ADA</b> yang lolos standar VIP.\n<i>(Sinyal dibuang karena berlawanan dengan Open Interest & Paus / Rawan Fakeout).</i>", chat_id)
        return

    send_telegram_message(f"🎯 <b>Filter Selesai!</b>\nDari {len(setups)} sinyal awal, ditemukan <b>{len(vip_signals_to_send)} Sinyal HIGH PROBABILITY</b> yang lolos standar VIP (Didukung OI/Paus). Mengirim grafik...", chat_id)

    for item in vip_signals_to_send:
        sym = item['sym']
        caption = item['caption']
        
        with data_lock: ohlcv = shared_ohlcv[sym][tf_request].copy()
            
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df = calculate_all_indicators(df)
        df = df.dropna().reset_index(drop=True)
        
        if len(df) > 10:
            photo_buffer = generate_candlestick_chart(sym, tf_request, df)
            send_telegram_photo(caption, photo_buffer, chat_id)
            time.sleep(1.5) 
        else:
            send_telegram_message(caption, chat_id)

def telegram_polling_thread():
    offset = None
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    print("📡 Bot Pendengar Telegram Aktif. Menunggu perintah user...")
    
    while True:
        try:
            params = {'timeout': 20}
            if offset: params['offset'] = offset
            
            req = requests.get(url, params=params, timeout=25)
            if not req.ok:
                time.sleep(2)
                continue

            response = req.json()

            if 'result' in response:
                for update in response['result']:
                    offset = update['update_id'] + 1
                    
                    msg_data = None
                    if 'message' in update: msg_data = update['message']
                    elif 'channel_post' in update: msg_data = update['channel_post']
                    elif 'edited_message' in update: msg_data = update['edited_message']
                    elif 'edited_channel_post' in update: msg_data = update['edited_channel_post']
                    else: continue
                    
                    if msg_data and 'text' in msg_data:
                        chat_id = msg_data['chat']['id']
                        text = msg_data['text'].strip().lower()

                        if 'status' in text or 'menu' in text or 'scan' in text:
                            reply_msg = get_summary_message()
                            send_telegram_message(reply_msg, target_chat_id=chat_id)
                            continue

                        tf_cmd = text.replace('/', '')
                        if tf_cmd in TIMEFRAMES:
                            threading.Thread(target=send_detailed_setups, args=(chat_id, tf_cmd)).start()
                        
        except requests.exceptions.ReadTimeout: pass 
        except Exception as e:
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

# ==========================================
# INDIKATOR & LOGIKA TRADING
# ==========================================
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
            is_new = False
            if past_pred is None: is_new = True
            elif past_pred['setup_name'] != setup_name: is_new = True
                
            if is_new:
                clean_sym = symbol.replace(':USDT', '')
                with print_lock:
                    print(f"   ✨ [SETUP BARU] {clean_sym} ({tf}) | {pred_dir} | {setup_name}")
            
            active_setups[tf][symbol] = {
                'dir': pred_dir, 
                'setup_name': setup_name,
                'price': current_candle['close'],
                'prev_price': closed_candle['close'],
                'stoch_k': current_candle['STOCH_K'],
                'stoch_d': current_candle['STOCH_D'],
                'ma5': current_candle['MA5_CLOSE'],
                'ma8': current_candle['MA8_CLOSE'],
                'ma13': current_candle['MA13_CLOSE'],
                'top_bb': current_candle['TOP_BB'],
                'low_bb': current_candle['LOW_BB']
            }
            locked_predictions[symbol][tf] = {'timestamp': current_candle['timestamp'], 'pred_dir': pred_dir, 'setup_name': setup_name}
        else:
            if symbol in active_setups[tf]:
                clean_sym = symbol.replace(':USDT', '')
                with print_lock:
                    print(f"   🗑️ [SETUP HILANG] {clean_sym} ({tf}) | Momentum tidak valid lagi.")
                del active_setups[tf][symbol]
                
            locked_predictions[symbol][tf] = None

    except Exception: pass

def initial_mass_scan():
    print("\n🔍 Memulai Pemindaian Massal (Mass Scan) seluruh koin...")
    send_telegram_message("⏳ <b>Menyiapkan Database Market...</b>\nBot sedang memindai seluruh 200+ koin Futures. Mohon tunggu sekitar 1 menit...")
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=15) as executor:
        futures = [executor.submit(process_data, sym, tf) for sym in SYMBOLS for tf in TIMEFRAMES]
        total = len(futures)
        for i, future in enumerate(concurrent.futures.as_completed(futures)):
            if i > 0 and i % 50 == 0:
                print(f"   [Progress Awal: {i}/{total} Tugas Selesai...]")
                
    print("✅ Mass Scan Selesai! Mengirim Laporan Ringkasan ke Telegram...")
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
    
    total_semua = benar_semua = salah_semua = 0
    for tf in TIMEFRAMES:
        res = results[tf]
        tot, bnr, slh = res['total'], res['benar'], res['salah']
        akurasi = (bnr / tot * 100) if tot > 0 else 0
        total_semua += tot; benar_semua += bnr; salah_semua += slh
        telegram_msg += f"{tf:<4} | {tot:<4} | {bnr:<4} | {slh:<4} | {akurasi:>6.1f}%\n"
        
    akurasi_total = (benar_semua / total_semua * 100) if total_semua > 0 else 0
    telegram_msg += "-" * 33 + "\n"
    telegram_msg += f"{'ALL':<4} | {total_semua:<4} | {benar_semua:<4} | {salah_semua:<4} | {akurasi_total:>6.1f}%\n</pre>"
    send_telegram_message(telegram_msg)
    input("Tekan [ENTER] untuk kembali ke Menu Utama...")

def run_bot():
    is_restored = initialize_memory()
    print("\n======================================================")
    print("🚀 MENJALANKAN BOT (FILTER SMART & VIP SIGNAL)")
    print("======================================================\n")

    if not is_restored:
        initial_mass_scan()
    else:
        send_telegram_message("🤖 <b>Bot Berhasil Direstart!</b>\nKetik <b>/status</b> untuk melihat ringkasan pasar.")

    start_websocket_thread()
    threading.Thread(target=telegram_polling_thread, daemon=True).start()

    global last_report_time
    last_report_time = datetime.now() 

    try:
        while True:
            cycle_start = time.time()
            total_tasks = len(SYMBOLS) * len(TIMEFRAMES)
            completed_tasks = 0
            
            with print_lock:
                print(f"\n🔄 [AUTO-SCAN] Memulai siklus pemindaian {len(SYMBOLS)} koin ({total_tasks} chart)...")

            def scan_and_count(sym, tf):
                nonlocal completed_tasks
                process_data(sym, tf)
                with print_lock:
                    completed_tasks += 1
                    if completed_tasks > 0 and completed_tasks % 100 == 0:
                        print(f"   ► Progress: {completed_tasks}/{total_tasks} chart dipindai...")

            with concurrent.futures.ThreadPoolExecutor(max_workers=15) as executor:
                futures = [executor.submit(scan_and_count, sym, tf) for sym in SYMBOLS for tf in TIMEFRAMES]
                concurrent.futures.wait(futures)
            
            cycle_duration = time.time() - cycle_start
            total_active = sum(len(active_setups[tf]) for tf in TIMEFRAMES)
            with print_lock:
                print(f"✅ [AUTO-SCAN] Selesai dalam {cycle_duration:.2f} detik. Total {total_active} setup ditahan di memori.")
            
            if (datetime.now() - last_report_time).total_seconds() >= 3600:
                send_hourly_report()
                manage_vps_storage()
                        
            time.sleep(15) 
            
    except KeyboardInterrupt:
        print("\n\n🛑 Bot dihentikan.")
        manage_vps_storage()
        send_telegram_message("🛑 <b>Bot dihentikan. Data telah dibackup dengan aman ke VPS.</b>")
        time.sleep(1) 

def main_menu():
    while True:
        os.system('cls' if os.name == 'nt' else 'clear') 
        print("======================================================")
        print("🌟 MENU UTAMA BOT (DENGAN SMART FILTER VIP) 🌟")
        print("======================================================")
        print("1. 🚀 Jalankan Auto-Scan (Log Terminal Aktif)")
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
