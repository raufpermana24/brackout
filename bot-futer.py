import ccxt
import pandas as pd
import numpy as np
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
# PENGATURAN BOT (TIMEFRAME SUDAH DIUPDATE)
# ==========================================
TIMEFRAMES = ['5m', '1h', '4h'] # 1m dan 15m telah dihapus
LIMIT = 100 

# Inisialisasi exchange Binance KHUSUS FUTURES
exchange = ccxt.binance({
    'apiKey': BINANCE_API_KEY,
    'secret': BINANCE_SECRET_KEY,
    'enableRateLimit': True,
    'options': {
        'defaultType': 'future'
    }
})

# ==========================================
# MEMORI TERKUNCI & STATISTIK (Dinamis)
# ==========================================
SYMBOLS = []
locked_predictions = {}
last_signaled_candle = {}
performance_stats = {tf: {'benar': 0, 'salah': 0} for tf in TIMEFRAMES}

last_report_time = datetime.now()
stats_lock = threading.Lock()     
print_lock = threading.Lock()     

def get_all_usdt_futures():
    """Mengambil semua daftar koin Futures berpasangan dengan USDT secara otomatis"""
    print("⏳ Mengambil daftar semua market USDT Futures aktif dari Binance...")
    try:
        markets = exchange.load_markets()
        symbols = []
        for symbol, market in markets.items():
            if market.get('active') and market.get('quote') == 'USDT' and market.get('contract'):
                symbols.append(symbol)
        print(f"✅ Berhasil menemukan {len(symbols)} market USDT Futures.")
        return symbols
    except Exception as e:
        print(f"⚠️ Gagal mengambil daftar market: {e}")
        return ['BTC/USDT:USDT'] # Fallback jika error

def initialize_memory():
    """Mempersiapkan memori untuk 200+ koin"""
    global SYMBOLS, locked_predictions, last_signaled_candle
    SYMBOLS = get_all_usdt_futures()
    locked_predictions = {sym: {tf: None for tf in TIMEFRAMES} for sym in SYMBOLS}
    last_signaled_candle = {sym: {tf: None for tf in TIMEFRAMES} for sym in SYMBOLS}

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
    table_str = "<b>📊 REKAP AKURASI BOT FUTURES (1 JAM) 📊</b>\n<pre>\n"
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
    table_str += "<i>*Seluruh Market: Segitiga, BBMA, Stoch, MA8/13, MA5.</i>"
    
    send_telegram_message(table_str)
    
    clean_terminal = table_str.replace("<b>", "").replace("</b>", "").replace("<pre>\n", "").replace("</pre>\n", "").replace("<i>", "").replace("</i>", "")
    with print_lock:
        print(f"\n======================================================\n{clean_terminal}\n======================================================\n")

def calculate_all_indicators(df):
    """MENGHITUNG SEMUA INDIKATOR"""
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
    """LOGIKA POLA SEGITIGA (TRIANGLE PATTERN)"""
    if len(df) < window + 2: return False, "", "NETRAL"
        
    curr = df.iloc[-1]
    prev = df.iloc[-2]
    
    highs = df['high'].iloc[-window-1:-1].values
    lows = df['low'].iloc[-window-1:-1].values
    
    x = np.arange(window)
    slope_high, _ = np.polyfit(x, highs, 1)
    slope_low, _ = np.polyfit(x, lows, 1)
    
    pct_slope_high = (slope_high / curr['close']) * 100
    pct_slope_low = (slope_low / curr['close']) * 100
    
    flat_thresh = 0.05 
    trend_thresh = 0.06
    
    is_high_flat = abs(pct_slope_high) < flat_thresh
    is_high_falling = pct_slope_high < -trend_thresh
    is_low_flat = abs(pct_slope_low) < flat_thresh
    is_low_rising = pct_slope_low > trend_thresh
    
    max_high_pola = np.max(highs)
    min_low_pola = np.min(lows)
    
    breakout_up = curr['close'] > max_high_pola and prev['close'] <= max_high_pola
    breakdown_down = curr['close'] < min_low_pola and prev['close'] >= min_low_pola

    if is_high_flat and is_low_rising and breakout_up:
        return True, "ASCENDING TRIANGLE BREAKOUT (Bullish) 🚀🔼", "UP"
    elif is_low_flat and is_high_falling and breakdown_down:
        return True, "DESCENDING TRIANGLE BREAKDOWN (Bearish) ☄️🔽", "DOWN"
    elif is_high_falling and is_low_rising:
        if breakout_up: return True, "SYMMETRICAL TRIANGLE BREAKOUT UP 🚀🔼", "UP"
        elif breakdown_down: return True, "SYMMETRICAL TRIANGLE BREAKDOWN DOWN ☄️🔽", "DOWN"

    return False, "", "NETRAL"

def detect_trading_setup(df):
    curr = df.iloc[-1]
    prev = df.iloc[-2]
    
    setup_name = "MENCARI SETUP ⚪"
    direction = "NETRAL"

    # 1. SEGITIGA
    is_triangle, tri_setup_name, tri_dir = detect_triangle_pattern(df)
    if is_triangle: return tri_setup_name, tri_dir

    # 2. BBMA EXTREME
    bbma_sell = curr['MA5_HIGH'] > curr['TOP_BB'] and curr['close'] <= curr['TOP_BB'] and curr['close'] < curr['open']
    bbma_buy = curr['MA5_LOW'] < curr['LOW_BB'] and curr['close'] >= curr['LOW_BB'] and curr['close'] > curr['open']

    # 3. STOCHASTIC CROSSOVER (<20 atau >80)
    stoch_sell = (prev['STOCH_K'] > prev['STOCH_D']) and (curr['STOCH_K'] < curr['STOCH_D']) and (curr['STOCH_K'] > 80)
    stoch_buy = (prev['STOCH_K'] < prev['STOCH_D']) and (curr['STOCH_K'] > curr['STOCH_D']) and (curr['STOCH_K'] < 20)

    # 4. MA8 & MA13 CROSSOVER
    ma813_cross_up = (prev['MA8_CLOSE'] <= prev['MA13_CLOSE']) and (curr['MA8_CLOSE'] > curr['MA13_CLOSE']) and (curr['close'] > curr['MA8_CLOSE'])
    ma813_cross_down = (prev['MA8_CLOSE'] >= prev['MA13_CLOSE']) and (curr['MA8_CLOSE'] < curr['MA13_CLOSE']) and (curr['close'] < curr['MA8_CLOSE'])

    # 5. MA5 CLOSE CROSSOVER
    ma5_cross_up = (prev['close'] <= prev['MA5_CLOSE']) and (curr['close'] > curr['MA5_CLOSE'])
    ma5_cross_down = (prev['close'] >= prev['MA5_CLOSE']) and (curr['close'] < curr['MA5_CLOSE'])

    # HIERARKI SINYAL
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
        
        eval_text += "❌ <b>Hasil: SALAH</b>. Penyebab Analisa Gagal:\n"
        
        is_triangle_eval = "TRIANGLE" in setup_name
        is_ma813_eval = "MA8/MA13" in setup_name
        is_ma5_eval = "MA5 CROSSOVER" in setup_name
        
        if pred_dir == "UP" and "DOWN" in actual_dir:
            if is_triangle_eval: eval_text += "👉 False Breakout (Bull Trap): Harga menembus atap segitiga lalu terbanting turun.\n"
            elif is_ma813_eval: eval_text += "👉 False Golden Cross: Momentum gagal dipertahankan setelah persilangan MA8/13.\n"
            elif is_ma5_eval: eval_text += "👉 False MA Breakout: Harga menembus MA5 tapi gagal menahan momentum naik.\n"
            else:
                if vol_spike: eval_text += "👉 Panic Sell merusak struktur Reversal.\n"
                else: eval_text += "👉 False Signal: Momentum turun masih kuat, setup batal.\n"
                
        elif pred_dir == "DOWN" and "UP" in actual_dir:
            if is_triangle_eval: eval_text += "👉 False Breakdown (Bear Trap): Harga jatuh menembus lantai tapi bandar memborong balik.\n"
            elif is_ma813_eval: eval_text += "👉 False Death Cross: Momentum turun ditolak pembeli setelah persilangan MA8/13.\n"
            elif is_ma5_eval: eval_text += "👉 False MA Breakdown: Harga menembus MA5 ke bawah tapi gagal menahan momentum turun.\n"
            else:
                if vol_spike: eval_text += "👉 Whale Buying (Paus) merusak struktur Reversal.\n"
                else: eval_text += "👉 False Signal: Momentum naik masih kuat, setup batal.\n"

    return eval_text

def process_data(symbol, tf):
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe=tf, limit=LIMIT)
        if not ohlcv or len(ohlcv) < 30: return 
            
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df = calculate_all_indicators(df)
        df = df.dropna().reset_index(drop=True)
        
        current_candle = df.iloc[-1]
        closed_candle = df.iloc[-2]
        
        open_price = current_candle['open']
        current_price = current_candle['close']
        
        setup_name, pred_dir = detect_trading_setup(df)
        
        eval_result_text = ""
        clean_symbol = symbol.replace(':USDT', '') 
        
        # --------------------------------------------------------
        # KIRIM SINYAL JIKA DITEMUKAN SETUP VALID
        # --------------------------------------------------------
        if last_signaled_candle[symbol][tf] != current_candle['timestamp']:
            telegram_message = ""
            
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
                
                stoch_status = "OVERBOUGHT (>80)" if current_candle['STOCH_K'] > 80 else ("OVERSOLD (<20)" if current_candle['STOCH_K'] < 20 else "NETRAL")
                telegram_message += "<i>Data Konfirmasi:</i>\n"
                telegram_message += f"• MA8: ${current_candle['MA8_CLOSE']:.2f} | MA13: ${current_candle['MA13_CLOSE']:.2f}\n"
                telegram_message += f"• Stoch %K: {current_candle['STOCH_K']:.1f} | %D: {current_candle['STOCH_D']:.1f}\n"
                telegram_message += f"• Status Stoch: {stoch_status}\n"
                telegram_message += f"• Top BB: ${current_candle['TOP_BB']:.2f} | Low BB: ${current_candle['LOW_BB']:.2f}\n"
                
                send_telegram_message(telegram_message)
                
                locked_predictions[symbol][tf] = {
                    'timestamp': current_candle['timestamp'],
                    'pred_dir': pred_dir,
                    'setup_name': setup_name
                }
            else:
                locked_predictions[symbol][tf] = None

            last_signaled_candle[symbol][tf] = current_candle['timestamp']

        # Tampilan Terminal Lokal (ANTI-SPAM)
        if eval_result_text or pred_dir != "NETRAL":
            price_diff = current_price - open_price
            diff_sym = "+" if price_diff >= 0 else ""
            
            terminal_output = ""
            if eval_result_text:
                terminal_output += eval_result_text + "\n"
            if pred_dir != "NETRAL":
                terminal_output += f"\n🪙 {clean_symbol} [{tf}]"
                terminal_output += f"\n  Open: ${open_price:.4f} | Now: ${current_price:.4f} ({diff_sym}{price_diff:.4f})"
                terminal_output += f"\n       ► Status Market : {setup_name}"

            with print_lock:
                print(terminal_output)

    except Exception as e:
        pass

# ==========================================
# BACKTEST (DIBATASI UNTUK 10 KOIN TERATAS)
# ==========================================
def run_backtest():
    initialize_memory()
    print("\n⏳ MENGAMBIL DATA 1 MINGGU KE BELAKANG UNTUK BACKTEST...")
    print("⚠️ Memproses sampel 10 Koin Populer dengan timeframe 5m, 1h, dan 4h...\n")
    
    send_telegram_message("⏳ <b>Proses Backtest Dimulai...</b>\nMensimulasikan Top 10 Koin Futures selama 1 minggu terakhir pada TF 5m, 1h, 4h.")
    
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
                    if pred_dir == actual_dir:
                        results[tf]['benar'] += 1
                    else:
                        results[tf]['salah'] += 1

    telegram_msg = "<b>📊 HASIL BACKTEST TOP 10 FUTURES (1 MINGGU) 📊</b>\n"
    telegram_msg += "<pre>\n"
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
        
    akurasi_total_semua = (benar_semua / total_semua * 100) if total_semua > 0 else 0
    telegram_msg += "-" * 33 + "\n"
    telegram_msg += f"{'ALL':<4} | {total_semua:<4} | {benar_semua:<4} | {salah_semua:<4} | {akurasi_total_semua:>6.1f}%\n</pre>"
    
    print("=======================================================================\n")
    send_telegram_message(telegram_msg)
    input("Tekan [ENTER] untuk kembali ke Menu Utama...")

def run_bot():
    initialize_memory()
    print("\n======================================================")
    print("🚀 MENJALANKAN BOT ALL FUTURES MARKET (Tekan Ctrl+C untuk Stop)")
    print("======================================================\n")

    send_telegram_message(f"🤖 <b>Bot Futures All Market Dimulai!</b>\nMendeteksi peluang LONG/SHORT di <b>{len(SYMBOLS)} Koin</b> secara bersamaan.\n(Timeframe aktif: 5m, 1h, 4h).")
    
    global last_report_time
    last_report_time = datetime.now() 

    try:
        while True:
            with concurrent.futures.ThreadPoolExecutor(max_workers=15) as executor:
                futures = [executor.submit(process_data, sym, tf) for sym in SYMBOLS for tf in TIMEFRAMES]
                concurrent.futures.wait(futures)
            
            if (datetime.now() - last_report_time).total_seconds() >= 3600:
                send_hourly_report()
                        
            time.sleep(30)
            
    except KeyboardInterrupt:
        print("\n\n🛑 Bot Real-Time dihentikan oleh pengguna.")
        send_telegram_message("🛑 <b>Bot dihentikan. Kembali ke mode Standby.</b>")
        time.sleep(1) 

def main_menu():
    while True:
        os.system('cls' if os.name == 'nt' else 'clear') 
        print("======================================================")
        print("🌟 MENU UTAMA BOT ALL FUTURES MARKET (200+ KOIN) 🌟")
        print("======================================================")
        print("1. 🚀 Jalankan Pendeteksi Seluruh Koin Futures Real-Time")
        print("2. 📊 Analisis Backtest (Sampel 10 Koin Populer)")
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
