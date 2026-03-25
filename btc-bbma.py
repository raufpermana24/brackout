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

# ==========================================
# MEMORI TERKUNCI & STATISTIK
# ==========================================
locked_predictions = {sym: {tf: None for tf in TIMEFRAMES} for sym in SYMBOLS}
last_signaled_candle = {sym: {tf: None for tf in TIMEFRAMES} for sym in SYMBOLS}
performance_stats = {tf: {'benar': 0, 'salah': 0} for tf in TIMEFRAMES}

last_report_time = datetime.now()
stats_lock = threading.Lock()     
print_lock = threading.Lock()     

def send_telegram_message(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        with print_lock:
            print(f"⚠️ Error koneksi Telegram: {e}")

def send_hourly_report():
    global last_report_time
    table_str = "<b>📊 REKAP AKURASI SNIPER EXTREME (1 JAM) 📊</b>\n<pre>\n"
    table_str += f"{'TF':<4} | {'BNR':<4} | {'SLH':<4} | {'AKURASI':<7}\n"
    table_str += "-" * 33 + "\n"
    
    total_benar = 0
    total_salah = 0
    
    with stats_lock:
        for tf in TIMEFRAMES:
            benar = performance_stats[tf]['benar']
            salah = performance_stats[tf]['salah']
            total = benar + salah
            akurasi = (benar / total * 100) if total > 0 else 0.0
            
            table_str += f"{tf:<4} | {benar:<4} | {salah:<4} | {akurasi:>6.1f}%\n"
            total_benar += benar
            total_salah += salah
            
            performance_stats[tf]['benar'] = 0
            performance_stats[tf]['salah'] = 0
        last_report_time = datetime.now()

    table_str += "-" * 33 + "\n"
    total_all = total_benar + total_salah
    total_akurasi = (total_benar / total_all * 100) if total_all > 0 else 0.0
    table_str += f"{'ALL':<4} | {total_benar:<4} | {total_salah:<4} | {total_akurasi:>6.1f}%\n</pre>\n"
    table_str += "<i>*Hanya menghitung saat Setup EXTREME tervalidasi.</i>"
    
    send_telegram_message(table_str)
    
    clean_terminal = table_str.replace("<b>", "").replace("</b>", "").replace("<pre>\n", "").replace("</pre>\n", "").replace("<i>", "").replace("</i>", "")
    with print_lock:
        print(f"\n======================================================\n{clean_terminal}\n======================================================\n")

def calculate_bbma_indicators(df):
    """MENGHITUNG INDIKATOR KHUSUS BBMA OMA ALLY SAJA"""
    df['MID_BB'] = df['close'].rolling(window=20).mean()
    df['STD_20'] = df['close'].rolling(window=20).std()
    df['TOP_BB'] = df['MID_BB'] + (df['STD_20'] * 2)
    df['LOW_BB'] = df['MID_BB'] - (df['STD_20'] * 2)

    df['MA5_HIGH'] = df['high'].rolling(window=5).mean()
    df['MA5_LOW'] = df['low'].rolling(window=5).mean()
    df['MA10_HIGH'] = df['high'].rolling(window=10).mean()
    df['MA10_LOW'] = df['low'].rolling(window=10).mean()

    df['MA50'] = df['close'].ewm(span=50, adjust=False).mean()
    df['AVG_VOL'] = df['volume'].rolling(window=20).mean()

    return df

def detect_bbma_setup(curr, prev):
    """
    LOGIKA SNIPER BBMA OMA ALLY
    HANYA mencari Setup EXTREME (Jenuh & Potensi Reversal)
    """
    setup_name = "MENCARI EXTREME ⚪"
    direction = "NETRAL"

    # ==========================================
    # SETUP EXTREME SAJA
    # Ciri: MA5 keluar dari BB (menandakan tren sangat jenuh), 
    # lalu harga close ditutup kembali ke dalam BB (rejeksi/awal reversal).
    # ==========================================
    if curr['MA5_HIGH'] > curr['TOP_BB'] and curr['close'] <= curr['TOP_BB'] and curr['close'] < curr['open']:
        return "EXTREME SELL (MA5 Keluar Top BB + Reversal) ⚠️🔴", "DOWN"
    elif curr['MA5_LOW'] < curr['LOW_BB'] and curr['close'] >= curr['LOW_BB'] and curr['close'] > curr['open']:
        return "EXTREME BUY (MA5 Keluar Low BB + Reversal) ⚠️🟢", "UP"

    # CSM, CSAK, MHV, Re-Entry dihilangkan. Bot diam jika tidak ada EXTREME.
    return setup_name, direction

def evaluate_past_prediction(symbol, tf, closed_candle, past_prediction):
    actual_dir = "UP 🟢" if closed_candle['close'] > closed_candle['open'] else "DOWN 🔴"
    pred_dir = past_prediction['pred_dir']
    setup_name = past_prediction['setup_name']
    
    pred_dir_icon = "UP 🟢" if pred_dir == "UP" else "DOWN 🔴"
    
    eval_text = f"<b>📊 EVALUASI {symbol} ({tf})</b>\nSetup: {setup_name}\nCandle Tutup: {actual_dir} | Prediksi Bot: {pred_dir_icon}\n"

    with stats_lock:
        if (actual_dir == "UP 🟢" and pred_dir == "UP") or (actual_dir == "DOWN 🔴" and pred_dir == "DOWN"):
            performance_stats[tf]['benar'] += 1
            return eval_text + "✅ <b>Hasil: BENAR</b>. (Reversal Terjadi!)"

        performance_stats[tf]['salah'] += 1
        
        body_size = abs(closed_candle['close'] - closed_candle['open'])
        up_w = closed_candle['high'] - max(closed_candle['open'], closed_candle['close'])
        dn_w = min(closed_candle['open'], closed_candle['close']) - closed_candle['low']
        vol_spike = closed_candle['volume'] > (closed_candle['AVG_VOL'] * 1.5)
        
        # Alasan kegagalan disesuaikan khusus untuk setup reversal (Extreme)
        eval_text += "❌ <b>Hasil: SALAH</b>. Penyebab Analisa Extreme Gagal:\n"
        if pred_dir == "UP" and "DOWN" in actual_dir:
            if vol_spike: eval_text += "👉 Panic Sell: Volume buangan mendadak merusak reversal.\n"
            else: eval_text += "👉 Momentum Turun Kuat: Harga belum benar-benar jenuh (Candle tembus Support).\n"
        elif pred_dir == "DOWN" and "UP" in actual_dir:
            if vol_spike: eval_text += "👉 Paus Masuk (Whale): Volume serok mendadak merusak reversal.\n"
            else: eval_text += "👉 Momentum Naik Kuat: Harga belum benar-benar jenuh (Candle tembus Resistensi).\n"

    return eval_text

def process_data(symbol, tf):
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe=tf, limit=LIMIT)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df = calculate_bbma_indicators(df)
        df = df.dropna().reset_index(drop=True)
        
        current_candle = df.iloc[-1]
        closed_candle = df.iloc[-2]
        
        open_price = current_candle['open']
        current_price = current_candle['close']
        
        # HANYA Deteksi Extreme
        setup_name, pred_dir = detect_bbma_setup(current_candle, closed_candle)
        
        eval_result_text = ""
        
        # --------------------------------------------------------
        # KIRIM SINYAL HANYA JIKA ADA SETUP EXTREME
        # --------------------------------------------------------
        if last_signaled_candle[symbol][tf] != current_candle['timestamp']:
            telegram_message = ""
            
            # Evaluasi Extreme sebelumnya (jika ada)
            past_pred = locked_predictions[symbol][tf]
            if past_pred is not None and past_pred['timestamp'] == closed_candle['timestamp']:
                eval_result = evaluate_past_prediction(symbol, tf, closed_candle, past_pred)
                telegram_message += eval_result + "\n\n"
                eval_result_text = f"\n{eval_result.replace('<b>', '').replace('</b>', '')}"
            
            # Jika EXTREME Ditemukan, tembak sinyal!
            if pred_dir != "NETRAL":
                telegram_message += f"<b>🚨 SETUP EXTREME DITEMUKAN: {symbol} ({tf})</b>\n"
                telegram_message += f"Kondisi: Tren Jenuh / Potensi Reversal\n"
                telegram_message += f"Harga Saat Ini: ${current_price:.4f}\n"
                telegram_message += f"Sinyal Setup: <b>{setup_name}</b>\n"
                telegram_message += f"Aksi Reversal: <b>{'BUY 🟢' if pred_dir == 'UP' else 'SELL 🔴'}</b>\n\n"
                
                telegram_message += "<i>Data Konfirmasi Extreme:</i>\n"
                telegram_message += f"• Top BB: ${current_candle['TOP_BB']:.2f}\n"
                telegram_message += f"• Mid BB (MA20): ${current_candle['MID_BB']:.2f}\n"
                telegram_message += f"• Low BB: ${current_candle['LOW_BB']:.2f}\n"
                telegram_message += f"• Posisi MA5: {'MA5 High > Top BB' if pred_dir == 'DOWN' else 'MA5 Low < Low BB'}\n"
                
                send_telegram_message(telegram_message)
                
                locked_predictions[symbol][tf] = {
                    'timestamp': current_candle['timestamp'],
                    'pred_dir': pred_dir,
                    'setup_name': setup_name
                }
            else:
                locked_predictions[symbol][tf] = None

            last_signaled_candle[symbol][tf] = current_candle['timestamp']

        # Tampilan Terminal Lokal
        price_diff = current_price - open_price
        diff_sym = "+" if price_diff >= 0 else ""
        
        terminal_output = ""
        if eval_result_text:
            terminal_output += eval_result_text + "\n"
        terminal_output += f"\n🪙 {symbol} [{tf}]"
        terminal_output += f"\n  Open: ${open_price:.4f} | Now: ${current_price:.4f} ({diff_sym}{price_diff:.4f})"
        terminal_output += f"\n       ► Status BBMA : {setup_name}"

        with print_lock:
            print(terminal_output)

    except Exception as e:
        with print_lock:
            print(f"\n🪙 {symbol} [{tf}] ⚠️ Error: {e}")

# ==========================================
# BACKTEST 1 MINGGU KHUSUS SETUP EXTREME
# ==========================================
def run_backtest():
    print("\n⏳ MENGAMBIL DATA 1 MINGGU KE BELAKANG UNTUK BACKTEST...")
    print("Sistem HANYA akan mencari momen Extreme (Jenuh) masa lalu...\n")
    
    send_telegram_message("⏳ <b>Proses Backtest Sniper Extreme Dimulai...</b>\nMencari semua setup Extreme (Reversal) BTC/USDT selama 1 minggu terakhir.")
    
    days = 7
    now_ms = exchange.milliseconds()
    start_ms = now_ms - (days * 24 * 60 * 60 * 1000)
    
    results = []

    for tf in TIMEFRAMES:
        print(f"🔄 Menganalisis Timeframe {tf}...")
        all_ohlcv = []
        since = start_ms
        
        while since < now_ms:
            try:
                ohlcv = exchange.fetch_ohlcv('BTC/USDT', timeframe=tf, since=since, limit=1000)
                if not ohlcv: break
                all_ohlcv.extend(ohlcv)
                since = ohlcv[-1][0] + 1
                time.sleep(0.1)
            except Exception as e:
                print(f"Error fetch: {e}")
                break
                
        if not all_ohlcv: continue
        
        df = pd.DataFrame(all_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df = calculate_bbma_indicators(df)
        df = df.dropna().reset_index(drop=True)
        
        benar = 0
        salah = 0

        for i in range(1, len(df)):
            curr = df.iloc[i-1] 
            prev = df.iloc[i-2] if i >= 2 else curr
            
            # Evaluasi menggunakan Logic Khusus Extreme
            setup_name, pred_dir = detect_bbma_setup(curr, prev)

            if pred_dir != "NETRAL":
                actual_dir = "UP" if df.iloc[i]['close'] > df.iloc[i]['open'] else "DOWN"
                if pred_dir == actual_dir:
                    benar += 1
                else:
                    salah += 1
                
        total = benar + salah
        akurasi = (benar / total * 100) if total > 0 else 0
        results.append((tf, total, benar, salah, akurasi))

    telegram_msg = "<b>📊 HASIL BACKTEST KHUSUS EXTREME (1 MINGGU) 📊</b>\n"
    telegram_msg += "Hanya menghitung sinyal Reversal BBMA\n"
    telegram_msg += "<pre>\n"
    telegram_msg += f"{'TF':<4} | {'TOT':<4} | {'BNR':<4} | {'SLH':<4} | {'AKURASI':<7}\n"
    telegram_msg += "-" * 33 + "\n"
    
    print("\n=======================================================================")
    print("📊 HASIL BACKTEST SNIPER EXTREME (1 MINGGU TERAKHIR)")
    print("=======================================================================")
    print(f"{'TIMEFRAME':<10} | {'SETUP EXTREME':<13} | {'BENAR':<8} | {'SALAH':<8} | {'AKURASI':<8}")
    print("-" * 69)
    
    total_semua = benar_semua = salah_semua = 0

    for res in results:
        tf, total, bnr, slh, akurasi = res
        total_semua += total; benar_semua += bnr; salah_semua += slh
        telegram_msg += f"{tf:<4} | {total:<4} | {bnr:<4} | {slh:<4} | {akurasi:>6.1f}%\n"
        print(f"{tf:<10} | {total:<13} | {bnr:<8} | {slh:<8} | {akurasi:.2f}%")
        
    akurasi_total_semua = (benar_semua / total_semua * 100) if total_semua > 0 else 0
    telegram_msg += "-" * 33 + "\n"
    telegram_msg += f"{'ALL':<4} | {total_semua:<4} | {benar_semua:<4} | {salah_semua:<4} | {akurasi_total_semua:>6.1f}%\n</pre>"
    
    print("=======================================================================\n")
    send_telegram_message(telegram_msg)
    input("Tekan [ENTER] untuk kembali ke Menu Utama...")

def run_bot():
    print("\n======================================================")
    print("🚀 MENJALANKAN BOT SNIPER EXTREME (Tekan Ctrl+C untuk Stop)")
    print("======================================================\n")

    send_telegram_message("🤖 <b>Bot Sniper Extreme Dimulai!</b>\nBot akan DIAM menunggu. Sinyal hanya dikirim jika MA keluar dari Top/Low BB (Kondisi Jenuh/Potensi Reversal)!")
    global last_report_time
    last_report_time = datetime.now() 

    try:
        while True:
            print(f"\n======================================================")
            print(f"🔄 Mencari Pola Jenuh (Extreme): {datetime.now().strftime('%H:%M:%S')}")
            print(f"======================================================")
            
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(SYMBOLS)*len(TIMEFRAMES)) as executor:
                futures = [executor.submit(process_data, sym, tf) for sym in SYMBOLS for tf in TIMEFRAMES]
                concurrent.futures.wait(futures)
            
            if (datetime.now() - last_report_time).total_seconds() >= 3600:
                send_hourly_report()
                        
            print("\n⏳ Menunggu 30 detik untuk scan berikutnya...")
            time.sleep(30)
    except KeyboardInterrupt:
        print("\n\n🛑 Bot Real-Time dihentikan oleh pengguna.")
        send_telegram_message("🛑 <b>Bot dihentikan. Kembali ke mode Standby.</b>")
        time.sleep(1) 

def main_menu():
    while True:
        os.system('cls' if os.name == 'nt' else 'clear') 
        print("======================================================")
        print("🌟 MENU UTAMA BOT CRYPTO SNIPER EXTREME (BBMA) 🌟")
        print("======================================================")
        print("1. 🚀 Jalankan Pendeteksi Extreme Real-Time")
        print("2. 📊 Analisis Backtest Extreme 1 Minggu Kebelakang")
        print("3. ❌ Keluar / Matikan Program")
        print("======================================================")
        
        pilihan = input("👉 Pilih menu (1/2/3): ")
        if pilihan == '1': run_bot()
        elif pilihan == '2': run_backtest()
        elif pilihan == '3':
            print("\nTerima kasih! Sampai jumpa.")
            break
        else:
            print("\n⚠️ Pilihan tidak valid.")
            time.sleep(2)

if __name__ == "__main__":
    main_menu()
