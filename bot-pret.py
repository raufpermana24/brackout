import ccxt
import pandas as pd
import time
from datetime import datetime
import os
import requests
import concurrent.futures
import threading

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
SYMBOLS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'XRP/USDT']
TIMEFRAMES = ['15m', '30m']
LIMIT = 100 

# Inisialisasi exchange Binance
exchange = ccxt.binance({
    'apiKey': BINANCE_API_KEY,
    'secret': BINANCE_SECRET_KEY,
    'enableRateLimit': True,
})

# Memori bot
prediction_history = {sym: {tf: None for tf in TIMEFRAMES} for sym in SYMBOLS}
last_signaled_candle = {sym: {tf: None for tf in TIMEFRAMES} for sym in SYMBOLS}

# Lock untuk mencegah teks di terminal bertabrakan saat threading
print_lock = threading.Lock()

def send_telegram_message(message):
    """Mengirim pesan ke Telegram"""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        response = requests.post(url, json=payload)
        if response.status_code != 200:
            with print_lock:
                print(f"⚠️ Gagal mengirim ke Telegram: {response.text}")
    except Exception as e:
        with print_lock:
            print(f"⚠️ Error koneksi Telegram: {e}")

def calculate_indicators(df):
    """Menghitung indikator teknikal (Lama + BBMA) - [TIDAK DIUBAH]"""
    df['EMA_9'] = df['close'].ewm(span=9, adjust=False).mean()
    df['EMA_21'] = df['close'].ewm(span=21, adjust=False).mean()

    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss.replace(0, 0.0001) 
    df['RSI'] = 100 - (100 / (1 + rs))
    df['AVG_VOL'] = df['volume'].rolling(window=20).mean()

    # Bollinger Bands & Moving Averages untuk BBMA
    df['SMA_20'] = df['close'].rolling(window=20).mean()
    df['STD_20'] = df['close'].rolling(window=20).std()
    df['BB_UPPER'] = df['SMA_20'] + (df['STD_20'] * 2)
    df['BB_LOWER'] = df['SMA_20'] - (df['STD_20'] * 2)

    df['MA5_HIGH'] = df['high'].rolling(window=5).mean()
    df['MA5_LOW'] = df['low'].rolling(window=5).mean()
    df['MA10_HIGH'] = df['high'].rolling(window=10).mean()
    df['MA10_LOW'] = df['low'].rolling(window=10).mean()
    df['MA50'] = df['close'].rolling(window=50).mean()

    return df

def analyze_bbma(df):
    """Setup Utama BBMA - [TIDAK DIUBAH]"""
    curr, prev = df.iloc[-1], df.iloc[-2]
    setup = "Konsolidasi / Belum ada Setup ⚪"

    if curr['close'] > curr['BB_UPPER']: setup = "Momentum BUY 🚀"
    elif curr['close'] < curr['BB_LOWER']: setup = "Momentum SELL ☄️"
    elif curr['MA5_HIGH'] > curr['BB_UPPER'] and curr['close'] <= curr['BB_UPPER']: setup = "Extreme SELL ⚠️🔴"
    elif curr['MA5_LOW'] < curr['BB_LOWER'] and curr['close'] >= curr['BB_LOWER']: setup = "Extreme BUY ⚠️🟢"
    elif prev['close'] > prev['BB_UPPER'] and curr['high'] <= curr['BB_UPPER']: setup = "MHV SELL 📉"
    elif prev['close'] < prev['BB_LOWER'] and curr['low'] >= curr['BB_LOWER']: setup = "MHV BUY 📈"
    elif curr['close'] > curr['MA50'] and (curr['low'] <= curr['MA5_LOW'] or curr['low'] <= curr['MA10_LOW']): setup = "Re-entry BUY 🎯🟢"
    elif curr['close'] < curr['MA50'] and (curr['high'] >= curr['MA5_HIGH'] or curr['high'] >= curr['MA10_HIGH']): setup = "Re-entry SELL 🎯🔴"

    return setup

def evaluate_past_prediction(symbol, tf, closed_candle, past_prediction):
    """Evaluasi memori lama - [TIDAK DIUBAH]"""
    actual_dir = "UP 🟢" if closed_candle['close'] > closed_candle['open'] else "DOWN 🔴"
    pred_dir = past_prediction['pred_dir']
    pred_dir_icon = "UP 🟢" if pred_dir == "UP" else "DOWN 🔴"
    
    eval_text = f"<b>📊 EVALUASI {symbol} ({tf})</b>\nCandle Tutup: {actual_dir} | Prediksi Bot: {pred_dir_icon}\n"

    if (actual_dir == "UP 🟢" and pred_dir == "UP") or (actual_dir == "DOWN 🔴" and pred_dir == "DOWN"):
        return eval_text + "✅ <b>Hasil: AKURAT</b>."

    eval_text += "❌ <b>Hasil: MISS</b>. Penyebab:\n"
    body_size = abs(closed_candle['close'] - closed_candle['open'])
    upper_wick = closed_candle['high'] - max(closed_candle['open'], closed_candle['close'])
    lower_wick = min(closed_candle['open'], closed_candle['close']) - closed_candle['low']
    vol_spike = closed_candle['volume'] > (closed_candle['AVG_VOL'] * 1.5)

    if pred_dir == "UP" and "DOWN" in actual_dir:
        if vol_spike: eval_text += "👉 Panic sell/Volume buang besar.\n"
        if upper_wick > body_size: eval_text += "👉 Rejection resistensi (Wick atas).\n"
        if "👉" not in eval_text: eval_text += "👉 Momentum berbalik (Fakeout).\n"
    elif pred_dir == "DOWN" and "UP" in actual_dir:
        if vol_spike: eval_text += "👉 Volume beli masif tiba-tiba.\n"
        if lower_wick > body_size: eval_text += "👉 Rejection support (Wick bawah).\n"
        if "👉" not in eval_text: eval_text += "👉 Teknikal rebound.\n"

    return eval_text

def process_data(symbol, tf):
    """Memproses data 1 koin (Akan dipanggil bersamaan oleh Threading)"""
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe=tf, limit=LIMIT)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df = calculate_indicators(df)
        
        current_candle = df.iloc[-1]
        closed_candle = df.iloc[-2]
        
        open_price = current_candle['open']
        current_price = current_candle['close']
        ema9, ema21, rsi = current_candle['EMA_9'], current_candle['EMA_21'], current_candle['RSI']

        # Prediksi Dasar
        pred_dir = "UP" if current_price > open_price else ("DOWN" if current_price < open_price else "NETRAL")
        candle_status = "UP 🟢" if pred_dir == "UP" and rsi < 70 else ("UP (Rawan Koreksi) 🟡" if pred_dir == "UP" else ("DOWN 🔴" if rsi > 30 else "DOWN (Rawan Mantul) 🟡"))
        if pred_dir == "NETRAL": candle_status = "NETRAL ⚪"

        trend_prediction = "Tren Naik 📈" if ema9 > ema21 else "Tren Turun 📉"
        bbma_setup = analyze_bbma(df)

        eval_result_text = ""
        
        # --------------------------------------------------------
        # TELEGRAM: Diproses saat ada pergantian Candle
        # --------------------------------------------------------
        if last_signaled_candle[symbol][tf] != current_candle['timestamp']:
            telegram_message = ""
            past_pred = prediction_history[symbol][tf]
            
            if past_pred is not None and past_pred['timestamp'] == closed_candle['timestamp']:
                eval_result = evaluate_past_prediction(symbol, tf, closed_candle, past_pred)
                telegram_message += eval_result + "\n\n"
                eval_result_text = f"\n{eval_result.replace('<b>', '').replace('</b>', '')}" # Disimpan untuk terminal
                
            telegram_message += f"<b>🔮 SINYAL BARU {symbol} ({tf})</b>\n"
            telegram_message += f"Harga Buka: ${open_price:.4f}\n"
            telegram_message += f"Sinyal Candle: {candle_status}\n"
            telegram_message += f"Sinyal Tren: {trend_prediction}\n"
            telegram_message += f"<b>Setup BBMA: {bbma_setup}</b>\n"
            telegram_message += f"RSI: {rsi:.2f}"
            
            send_telegram_message(telegram_message)
            
            last_signaled_candle[symbol][tf] = current_candle['timestamp']
            prediction_history[symbol][tf] = None

        if pred_dir != "NETRAL":
            prediction_history[symbol][tf] = {'timestamp': current_candle['timestamp'], 'pred_dir': pred_dir}

        # --------------------------------------------------------
        # TAMPILAN TERMINAL LOKAL (Menggunakan Lock agar rapi)
        # --------------------------------------------------------
        price_diff = current_price - open_price
        diff_sym = "+" if price_diff >= 0 else ""
        
        # Susun teks sebelum dicetak agar tidak terputus oleh thread lain
        terminal_output = ""
        if eval_result_text:
            terminal_output += eval_result_text + "\n"
        terminal_output += f"\n🪙 {symbol} [{tf}]"
        terminal_output += f"\n  Open: ${open_price:.4f} | Now: ${current_price:.4f} ({diff_sym}{price_diff:.4f})"
        terminal_output += f"\n       ► Prediksi : {candle_status} | Tren: {trend_prediction}"
        terminal_output += f"\n       ► Setup BBMA: {bbma_setup}"

        # Cetak menggunakan kunci (Lock)
        with print_lock:
            print(terminal_output)

    except Exception as e:
        with print_lock:
            print(f"\n🪙 {symbol} [{tf}] ⚠️ Error mengambil data: {e}")

def run_bot():
    print("======================================================")
    print("🚀 BOT CRYPTO TELEGRAM + BBMA (MULTITHREADING CEPAT) 🚀")
    print("======================================================\n")

    send_telegram_message("🤖 <b>Bot Prediksi (Fast Scan) Menyala!</b>\nSiap memantau BTC, ETH, SOL, XRP.\nTimeframe: 15m & 30m.")

    while True:
        print(f"\n\n======================================================")
        print(f"🔄 Memindai Semua Pasar Serentak: {datetime.now().strftime('%H:%M:%S')}")
        print(f"======================================================")
        
        # MENGGUNAKAN THREADING UNTUK SCAN PARALEL
        # Menjalankan 8 tugas (4 koin x 2 timeframe) dalam waktu bersamaan
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            futures = []
            for symbol in SYMBOLS:
                for tf in TIMEFRAMES:
                    # Serahkan tugas process_data ke thread pekerja
                    futures.append(executor.submit(process_data, symbol, tf))
            
            # Tunggu sampai semua thread selesai bekerja
            concurrent.futures.wait(futures)
                    
        print("\n⏳ Scan Selesai. Menunggu 30 detik untuk scan berikutnya...")
        time.sleep(30)

if __name__ == "__main__":
    try:
        run_bot()
    except KeyboardInterrupt:
        print("\nBot dimatikan.")
        send_telegram_message("🛑 <b>Bot dimatikan oleh admin.</b>")
