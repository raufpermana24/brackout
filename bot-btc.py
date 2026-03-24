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
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '-1003842052901')

# ==========================================
# PENGATURAN BOT (HANYA BTC)
# ==========================================
SYMBOLS = ['BTC/USDT']
TIMEFRAMES = ['1m', '5m', '15m', '1h', '4h']
# LIMIT dinaikkan menjadi 300 agar kalkulasi MACD dan ADX sangat akurat
LIMIT = 300 

exchange = ccxt.binance({
    'apiKey': BINANCE_API_KEY,
    'secret': BINANCE_SECRET_KEY,
    'enableRateLimit': True,
})

# MEMORI TERKUNCI & STATISTIK (Sistem Anti-Curang)
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
    table_str = "<b>📊 REKAP AKURASI PREDIKSI (1 JAM TERAKHIR) 📊</b>\n<pre>\n"
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
    table_str += f"{'TOTAL':<5} | {total_benar:<6} | {total_salah:<6} | {total_akurasi:>6.1f}%\n</pre>\n"
    table_str += "<i>*Sistem Anti-Curang Aktif (Lock Prediksi).</i>"
    
    send_telegram_message(table_str)
    
    clean_terminal = table_str.replace("<b>", "").replace("</b>", "").replace("<pre>\n", "").replace("</pre>\n", "").replace("<i>", "").replace("</i>", "")
    with print_lock:
        print(f"\n======================================================\n{clean_terminal}\n======================================================\n")

def calculate_indicators(df):
    """Menghitung Semua Indikator Dasar & Advance"""
    # 1. EMA & RSI
    df['EMA_9'] = df['close'].ewm(span=9, adjust=False).mean()
    df['EMA_21'] = df['close'].ewm(span=21, adjust=False).mean()
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss.replace(0, 0.0001) 
    df['RSI'] = 100 - (100 / (1 + rs))
    df['AVG_VOL'] = df['volume'].rolling(window=20).mean()

    # 2. BBMA
    df['SMA_20'] = df['close'].rolling(window=20).mean()
    df['STD_20'] = df['close'].rolling(window=20).std()
    df['BB_UPPER'] = df['SMA_20'] + (df['STD_20'] * 2)
    df['BB_LOWER'] = df['SMA_20'] - (df['STD_20'] * 2)
    df['MA5_HIGH'] = df['high'].rolling(window=5).mean()
    df['MA5_LOW'] = df['low'].rolling(window=5).mean()
    df['MA10_HIGH'] = df['high'].rolling(window=10).mean()
    df['MA10_LOW'] = df['low'].rolling(window=10).mean()
    df['MA50'] = df['close'].rolling(window=50).mean()

    # 3. MACD
    exp1 = df['close'].ewm(span=12, adjust=False).mean()
    exp2 = df['close'].ewm(span=26, adjust=False).mean()
    df['MACD'] = exp1 - exp2
    df['MACD_SIGNAL'] = df['MACD'].ewm(span=9, adjust=False).mean()

    # 4. Stochastic (14, 3, 3)
    low14 = df['low'].rolling(14).min()
    high14 = df['high'].rolling(14).max()
    df['STOCH_K'] = 100 * ((df['close'] - low14) / (high14 - low14).replace(0, 0.0001))
    df['STOCH_D'] = df['STOCH_K'].rolling(3).mean()

    # 5. ATR (Average True Range)
    tr0 = abs(df['high'] - df['low'])
    tr1 = abs(df['high'] - df['close'].shift())
    tr2 = abs(df['low'] - df['close'].shift())
    df['TR'] = pd.concat([tr0, tr1, tr2], axis=1).max(axis=1)
    df['ATR'] = df['TR'].rolling(14).mean()

    # 6. OBV (On-Balance Volume)
    df['OBV'] = (np.sign(df['close'].diff()) * df['volume']).fillna(0).cumsum()

    # 7. CMF (Chaikin Money Flow) 20-period
    mfm = ((df['close'] - df['low']) - (df['high'] - df['close'])) / (df['high'] - df['low']).replace(0, 0.0001)
    mfv = mfm * df['volume']
    df['CMF'] = mfv.rolling(20).sum() / df['volume'].rolling(20).sum().replace(0, 0.0001)

    # 8. SMI (Stochastic Momentum Index)
    c_m = df['close'] - (high14 + low14) / 2
    hl_diff = high14 - low14
    num = c_m.ewm(span=3, adjust=False).mean().ewm(span=3, adjust=False).mean()
    den = hl_diff.ewm(span=3, adjust=False).mean().ewm(span=3, adjust=False).mean() / 2
    df['SMI'] = 100 * (num / den.replace(0, 0.0001))
    df['SMI_SIGNAL'] = df['SMI'].ewm(span=3, adjust=False).mean()

    # 9. ADX (Average Directional Index)
    up = df['high'] - df['high'].shift(1)
    down = df['low'].shift(1) - df['low']
    df['+DM'] = np.where((up > down) & (up > 0), up, 0)
    df['-DM'] = np.where((down > up) & (down > 0), down, 0)
    tr_sm = df['TR'].ewm(span=14, adjust=False).mean()
    pdm_sm = df['+DM'].ewm(span=14, adjust=False).mean()
    mdm_sm = df['-DM'].ewm(span=14, adjust=False).mean()
    df['+DI'] = 100 * (pdm_sm / tr_sm.replace(0, 0.0001))
    df['-DI'] = 100 * (mdm_sm / tr_sm.replace(0, 0.0001))
    dx = 100 * abs(df['+DI'] - df['-DI']) / (df['+DI'] + df['-DI']).replace(0, 0.0001)
    df['ADX'] = dx.ewm(span=14, adjust=False).mean()

    return df

def analyze_bbma(df):
    curr, prev = df.iloc[-1], df.iloc[-2]
    setup = "Konsolidasi ⚪"
    if curr['close'] > curr['BB_UPPER']: setup = "Momentum BUY 🚀"
    elif curr['close'] < curr['BB_LOWER']: setup = "Momentum SELL ☄️"
    elif curr['MA5_HIGH'] > curr['BB_UPPER'] and curr['close'] <= curr['BB_UPPER']: setup = "Extreme SELL ⚠️🔴"
    elif curr['MA5_LOW'] < curr['BB_LOWER'] and curr['close'] >= curr['BB_LOWER']: setup = "Extreme BUY ⚠️🟢"
    elif prev['close'] > prev['BB_UPPER'] and curr['high'] <= curr['BB_UPPER']: setup = "MHV SELL 📉"
    elif prev['close'] < prev['BB_LOWER'] and curr['low'] >= curr['BB_LOWER']: setup = "MHV BUY 📈"
    elif curr['close'] > curr['MA50'] and (curr['low'] <= curr['MA5_LOW'] or curr['low'] <= curr['MA10_LOW']): setup = "Re-entry BUY 🎯🟢"
    elif curr['close'] < curr['MA50'] and (curr['high'] >= curr['MA5_HIGH'] or curr['high'] >= curr['MA10_HIGH']): setup = "Re-entry SELL 🎯🔴"
    return setup

def generate_master_score(df):
    """Menganalisa semua indikator sekaligus untuk Voting Naik/Turun"""
    curr, prev = df.iloc[-1], df.iloc[-2]
    score = 0
    reasons_up = []
    reasons_down = []

    # 1. Price Action (Harga Buka vs Harga Sekarang)
    if curr['close'] > curr['open']:
        score += 1
        reasons_up.append("PriceAction")
    else:
        score -= 1
        reasons_down.append("PriceAction")

    # 2. EMA Cross
    if curr['EMA_9'] > curr['EMA_21']:
        score += 1; reasons_up.append("EMA")
    else:
        score -= 1; reasons_down.append("EMA")

    # 3. MACD
    if curr['MACD'] > curr['MACD_SIGNAL']:
        score += 1; reasons_up.append("MACD")
    else:
        score -= 1; reasons_down.append("MACD")

    # 4. RSI
    if curr['RSI'] < 30:
        score += 2; reasons_up.append("RSI(Oversold)")
    elif curr['RSI'] > 70:
        score -= 2; reasons_down.append("RSI(Overbought)")
    elif curr['RSI'] > 50:
        score += 1; reasons_up.append("RSI(>50)")
    else:
        score -= 1; reasons_down.append("RSI(<50)")

    # 5. Stochastic
    if curr['STOCH_K'] > curr['STOCH_D']:
        if curr['STOCH_K'] < 20: score += 2; reasons_up.append("Stoch(Oversold)")
        else: score += 1; reasons_up.append("Stoch")
    else:
        if curr['STOCH_K'] > 80: score -= 2; reasons_down.append("Stoch(Overbought)")
        else: score -= 1; reasons_down.append("Stoch")

    # 6. OBV (Volume Tracker)
    if curr['OBV'] > prev['OBV']:
        score += 1; reasons_up.append("OBV(Akumulasi)")
    else:
        score -= 1; reasons_down.append("OBV(Distribusi)")

    # 7. CMF (Arus Uang)
    if curr['CMF'] > 0:
        score += 1; reasons_up.append("CMF(+)")
    else:
        score -= 1; reasons_down.append("CMF(-)")

    # 8. SMI (Stochastic Momentum Index)
    if curr['SMI'] > curr['SMI_SIGNAL']:
        score += 1; reasons_up.append("SMI")
    else:
        score -= 1; reasons_down.append("SMI")

    # 9. ADX (Kekuatan Tren)
    if curr['ADX'] > 25: # Tren Kuat
        if curr['+DI'] > curr['-DI']:
            score += 2; reasons_up.append("ADX(Strong UP)")
        else:
            score -= 2; reasons_down.append("ADX(Strong DOWN)")

    # Menentukan Prediksi Final berdasarkan Mayoritas Suara Indikator
    final_dir = "UP" if score >= 0 else "DOWN"
    
    return {
        'score': score,
        'final_dir': final_dir,
        'up_votes': len(reasons_up),
        'down_votes': len(reasons_down),
        'details_up': ", ".join(reasons_up),
        'details_down': ", ".join(reasons_down)
    }

def evaluate_past_prediction(symbol, tf, closed_candle, past_prediction):
    actual_dir = "UP 🟢" if closed_candle['close'] > closed_candle['open'] else "DOWN 🔴"
    pred_dir = past_prediction['pred_dir']
    pred_dir_icon = "UP 🟢" if pred_dir == "UP" else "DOWN 🔴"
    
    eval_text = f"<b>📊 EVALUASI {symbol} ({tf})</b>\nCandle Tutup: {actual_dir} | Prediksi Bot: {pred_dir_icon}\n"

    if (actual_dir == "UP 🟢" and pred_dir == "UP") or (actual_dir == "DOWN 🔴" and pred_dir == "DOWN"):
        with stats_lock:
            performance_stats[tf]['benar'] += 1
        return eval_text + "✅ <b>Hasil: BENAR</b>."

    with stats_lock:
        performance_stats[tf]['salah'] += 1

    eval_text += "❌ <b>Hasil: SALAH</b>. Penyebab:\n"
    body_size = abs(closed_candle['close'] - closed_candle['open'])
    upper_wick = closed_candle['high'] - max(closed_candle['open'], closed_candle['close'])
    lower_wick = min(closed_candle['open'], closed_candle['close']) - closed_candle['low']
    vol_spike = closed_candle['volume'] > (closed_candle['AVG_VOL'] * 1.5)

    if pred_dir == "UP" and "DOWN" in actual_dir:
        if vol_spike: eval_text += "👉 Volume Jual Dadakan (Paus buang barang).\n"
        if upper_wick > body_size: eval_text += "👉 Gagal tembus resistensi (Jarum atas).\n"
        if "👉" not in eval_text: eval_text += "👉 Momentum memudar drastis.\n"
    elif pred_dir == "DOWN" and "UP" in actual_dir:
        if vol_spike: eval_text += "👉 Volume Beli Dadakan (Paus serok).\n"
        if lower_wick > body_size: eval_text += "👉 Gagal tembus support (Jarum bawah).\n"
        if "👉" not in eval_text: eval_text += "👉 Terjadi teknikal pantulan mendadak.\n"

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
        
        # Menganalisa semua indikator bersamaan
        master_analysis = generate_master_score(df)
        pred_dir = master_analysis['final_dir']
        score = master_analysis['score']
        
        # Format Sinyal Tampilan
        if pred_dir == "UP":
            if score >= 5: candle_status = "STRONG UP 🚀"
            elif score >= 2: candle_status = "UP 🟢"
            else: candle_status = "WEAK UP 🟡"
        else:
            if score <= -5: candle_status = "STRONG DOWN ☄️"
            elif score <= -2: candle_status = "DOWN 🔴"
            else: candle_status = "WEAK DOWN 🟡"

        bbma_setup = analyze_bbma(df)
        eval_result_text = ""
        
        # --------------------------------------------------------
        # KIRIM KE TELEGRAM HANYA SAAT CANDLE BARU & KUNCI DATA
        # --------------------------------------------------------
        if last_signaled_candle[symbol][tf] != current_candle['timestamp']:
            telegram_message = ""
            
            # Evaluasi data terkunci
            past_pred = locked_predictions[symbol][tf]
            if past_pred is not None and past_pred['timestamp'] == closed_candle['timestamp']:
                eval_result = evaluate_past_prediction(symbol, tf, closed_candle, past_pred)
                telegram_message += eval_result + "\n\n"
                eval_result_text = f"\n{eval_result.replace('<b>', '').replace('</b>', '')}"
                
            telegram_message += f"<b>🔮 SINYAL KONSENSUS {symbol} ({tf})</b>\n"
            telegram_message += f"Prediksi Arah: <b>{candle_status}</b>\n"
            telegram_message += f"Skor Indikator: {score} poin\n\n"
            
            telegram_message += f"✅ <b>Pendukung NAIK ({master_analysis['up_votes']}):</b>\n{master_analysis['details_up']}\n\n"
            telegram_message += f"❌ <b>Pendukung TURUN ({master_analysis['down_votes']}):</b>\n{master_analysis['details_down']}\n\n"
            
            telegram_message += f"<b>Setup BBMA: {bbma_setup}</b>\n"
            telegram_message += f"Volatilitas (ATR): ${current_candle['ATR']:.2f}"
            
            send_telegram_message(telegram_message)
            
            # Mengunci prediksi untuk menjaga kejujuran bot
            locked_predictions[symbol][tf] = {
                'timestamp': current_candle['timestamp'],
                'pred_dir': pred_dir
            }
            last_signaled_candle[symbol][tf] = current_candle['timestamp']

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
        terminal_output += f"\n       ► Consensus   : {candle_status} (Skor: {score})"
        terminal_output += f"\n       ► Setup BBMA  : {bbma_setup}"

        with print_lock:
            print(terminal_output)

    except Exception as e:
        with print_lock:
            print(f"\n🪙 {symbol} [{tf}] ⚠️ Error: {e}")

def run_bot():
    print("======================================================")
    print("🚀 BOT CRYPTO ULTIMATE (MULTI-INDICATOR CONSENSUS) 🚀")
    print("======================================================\n")

    msg = ("🤖 <b>Bot Ultimate Menyala!</b>\n"
           "Memantau BTC/USDT (1m, 5m, 15m, 1h, 4h)\n\n"
           "🧠 <b>Otak Analisa:</b> Price, EMA, MACD, RSI, Stoch, OBV, CMF, SMI, ADX, ATR, & BBMA dihitung serentak!\n"
           "🔒 Fitur Anti-Curang Aktif.")
    send_telegram_message(msg)

    global last_report_time
    last_report_time = datetime.now() 

    while True:
        print(f"\n\n======================================================")
        print(f"🔄 Memantau BTC dengan 10+ Indikator Serentak: {datetime.now().strftime('%H:%M:%S')}")
        print(f"======================================================")
        
        total_tasks = len(SYMBOLS) * len(TIMEFRAMES)
        with concurrent.futures.ThreadPoolExecutor(max_workers=total_tasks) as executor:
            futures = [executor.submit(process_data, sym, tf) for sym in SYMBOLS for tf in TIMEFRAMES]
            concurrent.futures.wait(futures)
        
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
