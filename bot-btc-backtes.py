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
    table_str = "<b>📊 REKAP AKURASI BOT FINAL (1 JAM) 📊</b>\n<pre>\n"
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
    table_str += "<i>*Menghitung sinyal BBMA Extreme & Stochastic Cross.</i>"
    
    send_telegram_message(table_str)
    
    clean_terminal = table_str.replace("<b>", "").replace("</b>", "").replace("<pre>\n", "").replace("</pre>\n", "").replace("<i>", "").replace("</i>", "")
    with print_lock:
        print(f"\n======================================================\n{clean_terminal}\n======================================================\n")

def calculate_reversal_indicators(df):
    """MENGHITUNG BBMA OA & STOCHASTIC (5,3,3)"""
    # --- 1. BBMA ---
    df['MID_BB'] = df['close'].rolling(window=20).mean()
    df['STD_20'] = df['close'].rolling(window=20).std()
    df['TOP_BB'] = df['MID_BB'] + (df['STD_20'] * 2)
    df['LOW_BB'] = df['MID_BB'] - (df['STD_20'] * 2)

    df['MA5_HIGH'] = df['high'].rolling(window=5).mean()
    df['MA5_LOW'] = df['low'].rolling(window=5).mean()
    df['AVG_VOL'] = df['volume'].rolling(window=20).mean()

    # --- 2. STOCHASTIC (K=5, D=3, Smooth=3) ---
    low_min = df['low'].rolling(window=5).min()
    high_max = df['high'].rolling(window=5).max()
    fast_k = 100 * ((df['close'] - low_min) / (high_max - low_min).replace(0, 0.0001))
    
    df['STOCH_K'] = fast_k.rolling(window=3).mean()
    df['STOCH_D'] = df['STOCH_K'].rolling(window=3).mean()

    return df

def detect_reversal_setup(curr, prev):
    """
    LOGIKA REVERSAL KHUSUS (BBMA EXTREME & STOCHASTIC CROSSOVER)
    """
    setup_name = "MENCARI SETUP ⚪"
    direction = "NETRAL"

    # ==========================================
    # LOGIKA 1: BBMA EXTREME (TETAP JALAN)
    # ==========================================
    bbma_sell = curr['MA5_HIGH'] > curr['TOP_BB'] and curr['close'] <= curr['TOP_BB'] and curr['close'] < curr['open']
    bbma_buy = curr['MA5_LOW'] < curr['LOW_BB'] and curr['close'] >= curr['LOW_BB'] and curr['close'] > curr['open']

    # ==========================================
    # LOGIKA 2: STOCHASTIC CROSSOVER KHUSUS
    # Hanya kirim sinyal jika K memotong D di area <20 atau >80
    # ==========================================
    # Sell Cross: %K memotong ke bawah %D SAAT berada di area overbought (>80)
    stoch_sell = (prev['STOCH_K'] > prev['STOCH_D']) and (curr['STOCH_K'] < curr['STOCH_D']) and (curr['STOCH_K'] > 80)
    
    # Buy Cross: %K memotong ke atas %D SAAT berada di area oversold (<20)
    stoch_buy = (prev['STOCH_K'] < prev['STOCH_D']) and (curr['STOCH_K'] > curr['STOCH_D']) and (curr['STOCH_K'] < 20)

    # ==========================================
    # HIERARKI SINYAL TELEGRAM
    # ==========================================
    # 1. BERSAMAAN (Sangat Kuat)
    if bbma_sell and stoch_sell:
        return "PERFECT SELL (Extreme BBMA + Stoch Cross >80) 🌟🔴", "DOWN"
    elif bbma_buy and stoch_buy:
        return "PERFECT BUY (Extreme BBMA + Stoch Cross <20) 🌟🟢", "UP"
        
    # 2. BBMA EXTREME SAJA
    elif bbma_sell:
        return "EXTREME SELL (MA5 Keluar Top BB) ⚠️🔴", "DOWN"
    elif bbma_buy:
        return "EXTREME BUY (MA5 Keluar Low BB) ⚠️🟢", "UP"
        
    # 3. STOCHASTIC CROSSOVER SAJA (Sesuai Aturan Ketat Anda)
    elif stoch_sell:
        return "STOCH SELL (Garis %K Memotong %D ke Bawah di >80) 📉🔴", "DOWN"
    elif stoch_buy:
        return "STOCH BUY (Garis %K Memotong %D ke Atas di <20) 📈🟢", "UP"

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
            return eval_text + "✅ <b>Hasil: BENAR</b>. (Reversal/Crossover Valid!)"

        performance_stats[tf]['salah'] += 1
        
        body_size = abs(closed_candle['close'] - closed_candle['open'])
        up_w = closed_candle['high'] - max(closed_candle['open'], closed_candle['close'])
        dn_w = min(closed_candle['open'], closed_candle['close']) - closed_candle['low']
        vol_spike = closed_candle['volume'] > (closed_candle['AVG_VOL'] * 1.5)
        
        eval_text += "❌ <b>Hasil: SALAH</b>. Penyebab Analisa Gagal:\n"
        if pred_dir == "UP" and "DOWN" in actual_dir:
            if vol_spike: eval_text += "👉 Panic Sell merusak struktur Reversal.\n"
            else: eval_text += "👉 False Signal: Momentum turun masih kuat, setup batal.\n"
        elif pred_dir == "DOWN" and "UP" in actual_dir:
            if vol_spike: eval_text += "👉 Whale Buying (Paus) merusak struktur Reversal.\n"
            else: eval_text += "👉 False Signal: Momentum naik masih kuat, setup batal.\n"

    return eval_text

def process_data(symbol, tf):
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe=tf, limit=LIMIT)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df = calculate_reversal_indicators(df)
        df = df.dropna().reset_index(drop=True)
        
        current_candle = df.iloc[-1]
        closed_candle = df.iloc[-2]
        
        open_price = current_candle['open']
        current_price = current_candle['close']
        
        setup_name, pred_dir = detect_reversal_setup(current_candle, closed_candle)
        
        eval_result_text = ""
        
        # --------------------------------------------------------
        # KIRIM SINYAL HANYA JIKA ADA SETUP BBMA EXTREME / STOCH CROSSOVER
        # --------------------------------------------------------
        if last_signaled_candle[symbol][tf] != current_candle['timestamp']:
            telegram_message = ""
            
            past_pred = locked_predictions[symbol][tf]
            if past_pred is not None and past_pred['timestamp'] == closed_candle['timestamp']:
                eval_result = evaluate_past_prediction(symbol, tf, closed_candle, past_pred)
                telegram_message += eval_result + "\n\n"
                eval_result_text = f"\n{eval_result.replace('<b>', '').replace('</b>', '')}"
            
            if pred_dir != "NETRAL":
                telegram_message += f"<b>🚨 SETUP REVERSAL DITEMUKAN: {symbol} ({tf})</b>\n"
                telegram_message += f"Harga Saat Ini: ${current_price:.4f}\n"
                telegram_message += f"Sinyal Setup: <b>{setup_name}</b>\n"
                telegram_message += f"Aksi Direkomendasikan: <b>{'BUY 🟢' if pred_dir == 'UP' else 'SELL 🔴'}</b>\n\n"
                
                # Tentukan status Stoch untuk tampilan Telegram
                stoch_status = "OVERBOUGHT (>80)" if current_candle['STOCH_K'] > 80 else ("OVERSOLD (<20)" if current_candle['STOCH_K'] < 20 else "NETRAL")

                telegram_message += "<i>Data Konfirmasi:</i>\n"
                telegram_message += f"• Stoch %K(5,3): {current_candle['STOCH_K']:.1f}\n"
                telegram_message += f"• Stoch %D(3): {current_candle['STOCH_D']:.1f}\n"
                telegram_message += f"• Status Stoch: {stoch_status}\n"
                telegram_message += f"• Top BB: ${current_candle['TOP_BB']:.2f}\n"
                telegram_message += f"• Low BB: ${current_candle['LOW_BB']:.2f}\n"
                
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
        terminal_output += f"\n       ► Status Reversal : {setup_name}"
        terminal_output += f"\n       ► Stoch %K: {current_candle['STOCH_K']:.1f} | %D: {current_candle['STOCH_D']:.1f}"

        with print_lock:
            print(terminal_output)

    except Exception as e:
        with print_lock:
            print(f"\n🪙 {symbol} [{tf}] ⚠️ Error: {e}")

# ==========================================
# BACKTEST 1 MINGGU KHUSUS SETUP REVERSAL
# ==========================================
def run_backtest():
    print("\n⏳ MENGAMBIL DATA 1 MINGGU KE BELAKANG UNTUK BACKTEST...")
    print("Mencari momen BBMA Extreme & Stochastic Crossover (<20 / >80)...\n")
    
    send_telegram_message("⏳ <b>Proses Backtest Reversal Dimulai...</b>\nMencari semua setup Extreme BBMA & Stochastic Crossover BTC/USDT selama 1 minggu terakhir.")
    
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
        df = calculate_reversal_indicators(df)
        df = df.dropna().reset_index(drop=True)
        
        benar = 0
        salah = 0

        for i in range(1, len(df)):
            curr = df.iloc[i-1] 
            prev = df.iloc[i-2] if i >= 2 else curr
            
            setup_name, pred_dir = detect_reversal_setup(curr, prev)

            if pred_dir != "NETRAL":
                actual_dir = "UP" if df.iloc[i]['close'] > df.iloc[i]['open'] else "DOWN"
                if pred_dir == actual_dir:
                    benar += 1
                else:
                    salah += 1
                
        total = benar + salah
        akurasi = (benar / total * 100) if total > 0 else 0
        results.append((tf, total, benar, salah, akurasi))

    telegram_msg = "<b>📊 HASIL BACKTEST REVERSAL (1 MINGGU) 📊</b>\n"
    telegram_msg += "Filter: Extreme BBMA & Stoch Cross (<20 / >80)\n"
    telegram_msg += "<pre>\n"
    telegram_msg += f"{'TF':<4} | {'TOT':<4} | {'BNR':<4} | {'SLH':<4} | {'AKURASI':<7}\n"
    telegram_msg += "-" * 33 + "\n"
    
    print("\n=======================================================================")
    print("📊 HASIL BACKTEST REVERSAL (EXTREME BBMA + STOCH CROSSOVER)")
    print("=======================================================================")
    print(f"{'TIMEFRAME':<10} | {'TOTAL SETUP':<13} | {'BENAR':<8} | {'SALAH':<8} | {'AKURASI':<8}")
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
    print("🚀 MENJALANKAN BOT REVERSAL (Tekan Ctrl+C untuk Stop)")
    print("======================================================\n")

    send_telegram_message("🤖 <b>Bot Final Reversal Dimulai!</b>\nHanya mengirim sinyal saat terjadi BBMA Extreme ATAU Stochastic Crossover murni di area Overbought/Oversold.")
    global last_report_time
    last_report_time = datetime.now() 

    try:
        while True:
            print(f"\n======================================================")
            print(f"🔄 Mencari Pola Reversal Valid: {datetime.now().strftime('%H:%M:%S')}")
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
        print("🌟 MENU UTAMA BOT CRYPTO REVERSAL FINAL 🌟")
        print("======================================================")
        print("1. 🚀 Jalankan Pendeteksi BBMA Extreme & Stoch Crossover")
        print("2. 📊 Analisis Backtest 1 Minggu Kebelakang")
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
