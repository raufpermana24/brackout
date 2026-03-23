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
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '-1003842052901')

# ==========================================
# PENGATURAN BOT (HANYA BTC)
# ==========================================
SYMBOLS = ['BTC/USDT']
TIMEFRAMES = ['1m', '5m', '15m', '1h', '4h']
LIMIT = 100 

exchange = ccxt.binance({
    'apiKey': BINANCE_API_KEY,
    'secret': BINANCE_SECRET_KEY,
    'enableRateLimit': True,
})

# [BARU] MEMORI TERKUNCI: Menyimpan prediksi TEPAT SETELAH dikirim ke Telegram
locked_predictions = {sym: {tf: None for tf in TIMEFRAMES} for sym in SYMBOLS}
last_signaled_candle = {sym: {tf: None for tf in TIMEFRAMES} for sym in SYMBOLS}

# MEMORI STATISTIK 1 JAM
performance_stats = {tf: {'benar': 0, 'salah': 0} for tf in TIMEFRAMES}
last_report_time = datetime.now()
stats_lock = threading.Lock()     
print_lock = threading.Lock()     

def send_telegram_message(message):
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

def send_hourly_report():
    global last_report_time
    
    table_str = "<b>📊 REKAP AKURASI PREDIKSI (1 JAM TERAKHIR) 📊</b>\n"
    table_str += "<pre>\n"
    table_str += f"{'TF':<5} | {'BENAR':<6} | {'SALAH':<6} | {'AKURASI':<7}\n"
    table_str += "-" * 34 + "\n"
    
    total_benar = 0
    total_salah = 0
    
    with stats_lock:
        for tf in TIMEFRAMES:
            benar = performance_stats[tf]['benar']
            salah = performance_stats[tf]['salah']
            total = benar + salah
            akurasi = (benar / total * 100) if total > 0 else 0.0
            
            table_str += f"{tf:<5} | {benar:<6} | {salah:<6} | {akurasi:>6.1f}%\n"
            
            total_benar += benar
            total_salah += salah
            
            performance_stats[tf]['benar'] = 0
            performance_stats[tf]['salah'] = 0
        
        last_report_time = datetime.now()

    table_str += "-" * 34 + "\n"
    total_all = total_benar + total_salah
    total_akurasi = (total_benar / total_all * 100) if total_all > 0 else 0.0
    table_str += f"{'TOTAL':<5} | {total_benar:<6} | {total_salah:<6} | {total_akurasi:>6.1f}%\n"
    table_str += "</pre>\n"
    table_str += "<i>*Sistem Anti-Curang Aktif: Prediksi dikunci saat sinyal dikirim.</i>"
    
    send_telegram_message(table_str)
    
    clean_terminal_text = table_str.replace("<b>", "").replace("</b>", "").replace("<pre>\n", "").replace("</pre>\n", "").replace("<i>", "").replace("</i>", "")
    with print_lock:
        print(f"\n======================================================")
        print(clean_terminal_text)
        print(f"======================================================\n")

def calculate_indicators(df):
    df['EMA_9'] = df['close'].ewm(span=9, adjust=False).mean()
    df['EMA_21'] = df['close'].ewm(span=21, adjust=False).mean()

    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss.replace(0, 0.0001) 
    df['RSI'] = 100 - (100 / (1 + rs))
    df['AVG_VOL'] = df['volume'].rolling(window=20).mean()

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
    actual_dir = "UP 🟢" if closed_candle['close'] > closed_candle['open'] else "DOWN 🔴"
    
    # Membaca data yang sudah DIKUNCI
    pred_dir = past_prediction['pred_dir']
    pred_dir_icon = "UP 🟢" if pred_dir == "UP" else "DOWN 🔴"
    
    eval_text = f"<b>📊 EVALUASI {symbol} ({tf})</b>\nCandle Tutup: {actual_dir} | Prediksi Awal Bot: {pred_dir_icon}\n"

    if (actual_dir == "UP 🟢" and pred_dir == "UP") or (actual_dir == "DOWN 🔴" and pred_dir == "DOWN"):
        with stats_lock:
            performance_stats[tf]['benar'] += 1
        return eval_text + "✅ <b>Hasil: BENAR</b>."

    # Jika salah, bot wajib evaluasi penyebabnya (Tidak akan memalsukan data)
    with stats_lock:
        performance_stats[tf]['salah'] += 1

    eval_text += "❌ <b>Hasil: SALAH</b>. Penyebab:\n"
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
        
        # Jika candle baru mulai dan harga persis sama, kita gunakan tren EMA agar prediksi tidak NETRAL
        if pred_dir == "NETRAL":
            pred_dir = "UP" if ema9 > ema21 else "DOWN"

        candle_status = "UP 🟢" if pred_dir == "UP" and rsi < 70 else ("UP (Rawan Koreksi) 🟡" if pred_dir == "UP" else ("DOWN 🔴" if rsi > 30 else "DOWN (Rawan Mantul) 🟡"))
        
        trend_prediction = "Tren Naik 📈" if ema9 > ema21 else "Tren Turun 📉"
        bbma_setup = analyze_bbma(df)

        eval_result_text = ""
        
        # --------------------------------------------------------
        # KONDISI: CANDLE BARU TERBENTUK (Kirim Telegram & Kunci Data)
        # --------------------------------------------------------
        if last_signaled_candle[symbol][tf] != current_candle['timestamp']:
            telegram_message = ""
            
            # 1. EVALUASI PREDIKSI SEBELUMNYA (Berdasarkan data yang terkunci)
            past_pred = locked_predictions[symbol][tf]
            
            if past_pred is not None and past_pred['timestamp'] == closed_candle['timestamp']:
                eval_result = evaluate_past_prediction(symbol, tf, closed_candle, past_pred)
                telegram_message += eval_result + "\n\n"
                eval_result_text = f"\n{eval_result.replace('<b>', '').replace('</b>', '')}"
                
            # 2. KIRIM SINYAL BARU KE TELEGRAM
            telegram_message += f"<b>🔮 SINYAL BARU {symbol} ({tf})</b>\n"
            telegram_message += f"Harga Buka: ${open_price:.4f}\n"
            telegram_message += f"Sinyal Candle: {candle_status}\n"
            telegram_message += f"Sinyal Tren: {trend_prediction}\n"
            telegram_message += f"<b>Setup BBMA: {bbma_setup}</b>\n"
            telegram_message += f"RSI: {rsi:.2f}"
            
            send_telegram_message(telegram_message)
            
            # 3. KUNCI PREDIKSI! (Disimpan setelah dikirim, tidak akan diubah lagi)
            locked_predictions[symbol][tf] = {
                'timestamp': current_candle['timestamp'],
                'pred_dir': pred_dir
            }
            
            last_signaled_candle[symbol][tf] = current_candle['timestamp']

        # CATATAN PENTING:
        # Blok update prediksi yang berjalan setiap 30 detik (curang) telah DIHAPUS.
        # Data prediksi sekarang murni berdasarkan apa yang dikirim ke Telegram di awal candle.

        # --------------------------------------------------------
        # TAMPILAN TERMINAL LOKAL
        # --------------------------------------------------------
        price_diff = current_price - open_price
        diff_sym = "+" if price_diff >= 0 else ""
        
        terminal_output = ""
        if eval_result_text:
            terminal_output += eval_result_text + "\n"
        terminal_output += f"\n🪙 {symbol} [{tf}]"
        terminal_output += f"\n  Open: ${open_price:.4f} | Now: ${current_price:.4f} ({diff_sym}{price_diff:.4f})"
        terminal_output += f"\n       ► Arah Saat Ini : {candle_status} | Tren: {trend_prediction}"
        terminal_output += f"\n       ► Setup BBMA    : {bbma_setup}"

        with print_lock:
            print(terminal_output)

    except Exception as e:
        with print_lock:
            print(f"\n🪙 {symbol} [{tf}] ⚠️ Error mengambil data: {e}")

def run_bot():
    print("======================================================")
    print("🚀 BOT CRYPTO TELEGRAM + BBMA (JUJUR & ANTI-CURANG) 🚀")
    print("======================================================\n")

    send_telegram_message("🤖 <b>Bot Prediksi Jujur Menyala!</b>\nSiap memantau BTC/USDT.\nTimeframe: 1m, 5m, 15m, 1h, & 4h.\n\n🔒 <i>Prediksi dikunci otomatis setelah sinyal dikirim. Bot akan evaluasi 'SALAH' jika meleset.</i>")

    global last_report_time
    last_report_time = datetime.now() 

    while True:
        print(f"\n\n======================================================")
        print(f"🔄 Memantau BTC Serentak: {datetime.now().strftime('%H:%M:%S')}")
        print(f"======================================================")
        
        total_tasks = len(SYMBOLS) * len(TIMEFRAMES)
        with concurrent.futures.ThreadPoolExecutor(max_workers=total_tasks) as executor:
            futures = []
            for symbol in SYMBOLS:
                for tf in TIMEFRAMES:
                    futures.append(executor.submit(process_data, symbol, tf))
            
            concurrent.futures.wait(futures)
        
        # CEK REKAP 1 JAM (3600 Detik)
        time_elapsed = (datetime.now() - last_report_time).total_seconds()
        if time_elapsed >= 3600:
            send_hourly_report()
                    
        print("\n⏳ Menunggu 30 detik untuk scan berikutnya...")
        time.sleep(30)

if __name__ == "__main__":
    try:
        run_bot()
    except KeyboardInterrupt:
        print("\nBot dimatikan.")
        send_telegram_message("🛑 <b>Bot dimatikan oleh admin.</b>")
