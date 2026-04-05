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
# PENGATURAN BOT
# ==========================================
TIMEFRAMES = ['5m', '1h', '4h'] 
LIMIT = 100 

exchange = ccxt.binance({
    'apiKey': BINANCE_API_KEY,
    'secret': BINANCE_SECRET_KEY,
    'enableRateLimit': True,
    'options': {'defaultType': 'future'}
})

# ==========================================
# MEMORI TERKUNCI, STATISTIK & WEBSOCKET
# ==========================================
SYMBOLS = []
locked_predictions = {}
last_signaled_candle = {}
performance_stats = {tf: {'benar': 0, 'salah': 0} for tf in TIMEFRAMES}
last_imbalance = {}

# [BARU] In-Memory Database untuk Data WebSocket
shared_ohlcv = {}
ccxt_to_ws_sym = {}
ws_to_ccxt_sym = {}

last_report_time = datetime.now()
stats_lock = threading.Lock()     
print_lock = threading.Lock()
data_lock = threading.Lock() # Kunci untuk sinkronisasi WebSocket & REST     

def get_all_usdt_futures():
    """Mengambil semua daftar koin Futures"""
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
    """Mempersiapkan database di memori RAM"""
    global SYMBOLS, locked_predictions, last_signaled_candle, last_imbalance, shared_ohlcv
    SYMBOLS = get_all_usdt_futures()
    locked_predictions = {sym: {tf: None for tf in TIMEFRAMES} for sym in SYMBOLS}
    last_signaled_candle = {sym: {tf: None for tf in TIMEFRAMES} for sym in SYMBOLS}
    last_imbalance = {sym: 50.0 for sym in SYMBOLS}
    shared_ohlcv = {sym: {tf: [] for tf in TIMEFRAMES} for sym in SYMBOLS}
    
    # Mapping nama koin CCXT (BTC/USDT:USDT) ke Binance WS (btcusdt)
    for sym in SYMBOLS:
        ws_sym = sym.split(':')[0].replace('/', '').lower()
        ccxt_to_ws_sym[sym] = ws_sym
        ws_to_ccxt_sym[ws_sym] = sym

# ==========================================
# [BARU] ENGINE WEBSOCKET HYBRID
# ==========================================
def ws_on_message(ws, message):
    """Menerima live-streaming Candlestick dari Binance"""
    try:
        data = json.loads(message)
        if 'data' in data and 'k' in data['data']:
            kline = data['data']['k']
            sym_ws = kline['s'].lower()
            tf = kline['i']
            
            if sym_ws in ws_to_ccxt_sym and tf in TIMEFRAMES:
                sym_ccxt = ws_to_ccxt_sym[sym_ws]
                
                # Format CCXT [timestamp, open, high, low, close, volume]
                candle = [
                    kline['t'], 
                    float(kline['o']), float(kline['h']), 
                    float(kline['l']), float(kline['c']), float(kline['v'])
                ]
                
                with data_lock:
                    history = shared_ohlcv[sym_ccxt][tf]
                    if not history:
                        history.append(candle)
                    else:
                        last_t = history[-1][0]
                        if candle[0] == last_t:
                            history[-1] = candle # Update candle berjalan
                        elif candle[0] > last_t:
                            history.append(candle) # Tambah candle baru
                            if len(history) > LIMIT:
                                history.pop(0) # Jaga memori tetap ringan
    except Exception:
        pass

def ws_on_open(ws):
    """Subscribe ke ratusan koin saat koneksi terbuka"""
    print("📡 Terhubung ke Binance WebSocket! Berlangganan Stream Data...")
    streams = []
    for sym in SYMBOLS:
        ws_sym = ccxt_to_ws_sym[sym]
        for tf in TIMEFRAMES:
            streams.append(f"{ws_sym}@kline_{tf}")
    
    # Binance membatasi max 50 stream per request, kita pecah (chunking)
    chunk_size = 50
    for i in range(0, len(streams), chunk_size):
        chunk = streams[i:i+chunk_size]
        payload = {"method": "SUBSCRIBE", "params": chunk, "id": i}
        ws.send(json.dumps(payload))
        time.sleep(0.3)
    print("✅ Berhasil Berlangganan Live Data WebSocket!")

def start_websocket_thread():
    """Menjalankan WebSocket di latar belakang"""
    url = "wss://fstream.binance.com/stream"
    ws = websocket.WebSocketApp(url, on_message=ws_on_message, on_open=ws_on_open)
    
    def run():
        while True:
            ws.run_forever()
            time.sleep(5) # Reconnect jika putus
            
    t = threading.Thread(target=run, daemon=True)
    t.start()

# ==========================================
# FUNGSI TELEGRAM & HEATMAP (ORDER BOOK)
# ==========================================
def send_telegram_message(message, photo_buf=None):
    try:
        if photo_buf:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
            files = {'photo': ('heatmap.png', photo_buf, 'image/png')}
            data = {'chat_id': TELEGRAM_CHAT_ID, 'caption': message, 'parse_mode': 'HTML'}
            requests.post(url, files=files, data=data, timeout=15)
        else:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
            requests.post(url, json=payload, timeout=10)
    except Exception as e:
        pass

def analyze_order_book(symbol):
    try:
        ob = exchange.fetch_order_book(symbol, limit=100)
        bids, asks = ob['bids'], ob['asks']
        if not bids or not asks: return None, None

        total_bid_vol = sum(bid[1] for bid in bids)
        total_ask_vol = sum(ask[1] for ask in asks)
        total_vol = total_bid_vol + total_ask_vol
        buy_imbalance = (total_bid_vol / total_vol) * 100 if total_vol > 0 else 50
        sell_imbalance = 100 - buy_imbalance

        prev_imbalance = last_imbalance.get(symbol, 50.0)
        shift_diff = buy_imbalance - prev_imbalance
        if shift_diff > 5: sentiment_shift = "🟢 Paus Menumpuk Order Beli (Bullish Shift)"
        elif shift_diff < -5: sentiment_shift = "🔴 Paus Menumpuk Order Jual (Bearish Shift)"
        else: sentiment_shift = "⚪ Stabil"
        last_imbalance[symbol] = buy_imbalance

        max_bid = max(bids, key=lambda x: x[1])
        max_ask = max(asks, key=lambda x: x[1])

        ob_text = f"<b>🐋 WHALE TRACKER (ORDER BOOK)</b>\n"
        ob_text += f"• Imbalance: {buy_imbalance:.1f}% Beli / {sell_imbalance:.1f}% Jual\n"
        ob_text += f"• Sentimen Paus: {sentiment_shift}\n"
        ob_text += f"• Tembok Support (Beli): ${max_bid[0]:.4f} (Vol: {max_bid[1]:.1f})\n"
        ob_text += f"• Tembok Resist (Jual): ${max_ask[0]:.4f} (Vol: {max_ask[1]:.1f})\n"

        # Gambar Heatmap
        bid_prices, bid_vols_cum = [x[0] for x in bids], np.cumsum([x[1] for x in bids])
        ask_prices, ask_vols_cum = [x[0] for x in asks], np.cumsum([x[1] for x in asks])

        fig, ax = plt.subplots(figsize=(8, 4))
        fig.patch.set_facecolor('#1e1e2f')
        ax.set_facecolor('#1e1e2f')

        ax.fill_between(bid_prices, bid_vols_cum, color='#00ff88', alpha=0.5, step="pre", label='Bids (Support Wall)')
        ax.fill_between(ask_prices, ask_vols_cum, color='#ff3366', alpha=0.5, step="pre", label='Asks (Resistance Wall)')

        clean_sym = symbol.replace(':USDT', '')
        ax.set_title(f"Liquidity Depth Heatmap - {clean_sym}", color='white', fontsize=12)
        ax.set_xlabel("Harga Jual/Beli", color='white')
        ax.set_ylabel("Akumulasi Volume", color='white')
        ax.tick_params(colors='white')
        ax.legend(loc='upper right', facecolor='#2a2a3f', labelcolor='white')
        plt.tight_layout()

        buf = io.BytesIO()
        plt.savefig(buf, format='png', facecolor=fig.get_facecolor(), edgecolor='none')
        buf.seek(0)
        plt.close(fig)

        return ob_text, buf
    except Exception:
        return None, None

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
    with print_lock:
        print(f"\n======================================================\n{clean_terminal}\n======================================================\n")

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
    setup_name = past_prediction['setup_name']
    
    clean_symbol = symbol.replace(':USDT', '')
    pred_dir_icon = "UP 🟢" if pred_dir == "UP" else "DOWN 🔴"
    
    eval_text = f"<b>📊 EVALUASI {clean_symbol} ({tf})</b>\nSetup: {setup_name}\nCandle Tutup: {actual_dir} | Prediksi Bot: {pred_dir_icon}\n"

    with stats_lock:
        if (actual_dir == "UP 🟢" and pred_dir == "UP") or (actual_dir == "DOWN 🔴" and pred_dir == "DOWN"):
            performance_stats[tf]['benar'] += 1
            return eval_text + "✅ <b>Hasil: BENAR</b>. (Arah pergerakan Valid!)"

        performance_stats[tf]['salah'] += 1
        
        body_size = abs(closed_candle['close'] - closed_candle['open'])
        up_w = closed_candle['high'] - max(closed_candle['open'], closed_candle['close'])
        dn_w = min(closed_candle['open'], closed_candle['close']) - closed_candle['low']
        vol_spike = closed_candle['volume'] > (closed_candle['AVG_VOL'] * 1.5)
        
        eval_text += "❌ <b>Hasil: SALAH</b>. Penyebab:\n"
        is_triangle = "TRIANGLE" in setup_name
        is_ma813 = "MA8/MA13" in setup_name
        is_ma5 = "MA5 CROSSOVER" in setup_name
        
        if pred_dir == "UP" and "DOWN" in actual_dir:
            if is_triangle: eval_text += "👉 False Breakout: Ditolak Tembok Jual Paus.\n"
            elif is_ma813: eval_text += "👉 False Golden Cross: Momentum mati mendadak.\n"
            elif is_ma5: eval_text += "👉 False MA Breakout: Harga gagal ditahan pembeli.\n"
            else: eval_text += "👉 Panic Sell merusak struktur Reversal.\n" if vol_spike else "👉 False Signal: Indikator menipu.\n"
                
        elif pred_dir == "DOWN" and "UP" in actual_dir:
            if is_triangle: eval_text += "👉 False Breakdown: Tembus support tapi Paus serok.\n"
            elif is_ma813: eval_text += "👉 False Death Cross: Penurunan ditolak pembeli.\n"
            elif is_ma5: eval_text += "👉 False MA Breakdown: Harga gagal jatuh.\n"
            else: eval_text += "👉 Whale Buying membatalkan Reversal Sell.\n" if vol_spike else "👉 False Signal: Indikator menipu.\n"

    return eval_text

def process_data(symbol, tf):
    try:
        # ==========================================
        # [PENTING] LOGIKA HYBRID (WS + REST FALLBACK)
        # ==========================================
        with data_lock:
            ohlcv = shared_ohlcv[symbol][tf].copy()
            
        # Jika data WebSocket belum lengkap (Minimal butuh 30 candle untuk MA dan BB)
        if len(ohlcv) < 30:
            try:
                # Fallback: Ambil data masa lalu pakai REST API CCXT
                ohlcv = exchange.fetch_ohlcv(symbol, timeframe=tf, limit=LIMIT)
                with data_lock:
                    shared_ohlcv[symbol][tf] = ohlcv # Sinkronkan ke Memori WS
            except Exception:
                return # Skip jika REST gagal
                
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df = calculate_all_indicators(df)
        df = df.dropna().reset_index(drop=True)
        if len(df) < 5: return
        
        current_candle = df.iloc[-1]
        closed_candle = df.iloc[-2]
        open_price, current_price = current_candle['open'], current_candle['close']
        
        setup_name, pred_dir = detect_trading_setup(df)
        
        eval_result_text = ""
        clean_symbol = symbol.replace(':USDT', '') 
        
        # --------------------------------------------------------
        # KIRIM SINYAL JIKA DITEMUKAN SETUP VALID
        # --------------------------------------------------------
        if last_signaled_candle[symbol][tf] != current_candle['timestamp']:
            telegram_message = ""
            photo_buffer = None
            
            past_pred = locked_predictions[symbol][tf]
            if past_pred is not None and past_pred['timestamp'] == closed_candle['timestamp']:
                eval_result = evaluate_past_prediction(symbol, tf, closed_candle, past_pred)
                telegram_message += eval_result + "\n\n"
                eval_result_text = f"\n{eval_result.replace('<b>', '').replace('</b>', '')}"
            
            if pred_dir != "NETRAL":
                if "TRIANGLE" in setup_name: telegram_message += f"<b>📐 SETUP POLA SEGITIGA: {clean_symbol} ({tf})</b>\n"
                elif "MA8/MA13" in setup_name: telegram_message += f"<b>⚔️ PERSILANGAN MA8/MA13: {clean_symbol} ({tf})</b>\n"
                elif "MA5" in setup_name: telegram_message += f"<b>⚡ MOMENTUM MA5 CLOSE: {clean_symbol} ({tf})</b>\n"
                else: telegram_message += f"<b>🚨 SETUP REVERSAL: {clean_symbol} ({tf})</b>\n"
                    
                telegram_message += f"Harga Saat Ini: ${current_price:.4f}\n"
                telegram_message += f"Sinyal Setup: <b>{setup_name}</b>\n"
                telegram_message += f"Aksi Futures: <b>{'LONG 🟢' if pred_dir == 'UP' else 'SHORT 🔴'}</b>\n\n"
                
                # --- PANGGIL ANALISIS ORDER BOOK & GAMBAR HEATMAP ---
                ob_text, photo_buffer = analyze_order_book(symbol)
                if ob_text: telegram_message += ob_text + "\n"
                
                stoch_status = "OVERBOUGHT (>80)" if current_candle['STOCH_K'] > 80 else ("OVERSOLD (<20)" if current_candle['STOCH_K'] < 20 else "NETRAL")
                telegram_message += "<i>Data Konfirmasi Teknikal:</i>\n"
                telegram_message += f"• Stoch %K: {current_candle['STOCH_K']:.1f} | %D: {current_candle['STOCH_D']:.1f}\n"
                telegram_message += f"• Top BB: ${current_candle['TOP_BB']:.2f} | Low BB: ${current_candle['LOW_BB']:.2f}\n"
                
                send_telegram_message(telegram_message, photo_buffer)
                
                locked_predictions[symbol][tf] = {'timestamp': current_candle['timestamp'], 'pred_dir': pred_dir, 'setup_name': setup_name}
            else:
                locked_predictions[symbol][tf] = None

            last_signaled_candle[symbol][tf] = current_candle['timestamp']

        # Tampilan Terminal (ANTI-SPAM)
        if eval_result_text or pred_dir != "NETRAL":
            diff_sym = "+" if (current_price - open_price) >= 0 else ""
            terminal_output = ""
            if eval_result_text: terminal_output += eval_result_text + "\n"
            if pred_dir != "NETRAL":
                terminal_output += f"\n🪙 {clean_symbol} [{tf}]"
                terminal_output += f"\n  Open: ${open_price:.4f} | Now: ${current_price:.4f} ({diff_sym}{current_price - open_price:.4f})"
                terminal_output += f"\n       ► Status Market : {setup_name}"

            with print_lock:
                print(terminal_output)

    except Exception:
        pass

# ==========================================
# BACKTEST (SAMPEL)
# ==========================================
def run_backtest():
    initialize_memory()
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
                except Exception:
                    break
                    
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
    initialize_memory()
    print("\n======================================================")
    print("🚀 MENJALANKAN BOT HYBRID (WEBSOCKET + REST FALLBACK)")
    print("======================================================\n")

    # Memulai benang (thread) WebSocket di Background
    start_websocket_thread()

    send_telegram_message(f"🤖 <b>Bot Ultimate Hybrid Dimulai!</b>\nMemantau {len(SYMBOLS)} koin via Live WebSockets (Bebas Lag & Limit API).")
    
    global last_report_time
    last_report_time = datetime.now() 

    try:
        while True:
            # Karena data sudah diupdate instan oleh WebSocket, 
            # Thread pool ini tugasnya hanya mengecek memori & menghitung sinyal
            with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
                futures = [executor.submit(process_data, sym, tf) for sym in SYMBOLS for tf in TIMEFRAMES]
                concurrent.futures.wait(futures)
            
            if (datetime.now() - last_report_time).total_seconds() >= 3600:
                send_hourly_report()
                        
            time.sleep(10) # Lebih agresif karena membaca RAM lokal, bukan memanggil API terus menerus
            
    except KeyboardInterrupt:
        print("\n\n🛑 Bot dihentikan.")
        send_telegram_message("🛑 <b>Bot dihentikan. Kembali ke mode Standby.</b>")
        time.sleep(1) 

def main_menu():
    while True:
        os.system('cls' if os.name == 'nt' else 'clear') 
        print("======================================================")
        print("🌟 MENU UTAMA BOT ALL FUTURES HYBRID WS 🌟")
        print("======================================================")
        print("1. 🚀 Jalankan Pendeteksi Real-Time (WebSocket)")
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
