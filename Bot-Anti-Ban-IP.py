import sys
import os
import time
import random
from datetime import datetime
import concurrent.futures
import warnings
import numpy as np

# Filter warning agar log bersih
warnings.filterwarnings("ignore", category=UserWarning)

# ==========================================
# 1. CEK LIBRARY
# ==========================================
try:
    import ccxt
    import pandas as pd
    import pandas_ta as ta
    import mplfinance as mpf
    import requests
    
    # --- FIX CHART UNTUK VPS/SERVER ---
    import matplotlib
    matplotlib.use('Agg') # Mode tanpa layar (Headless)
    import matplotlib.pyplot as plt
    # ----------------------------------

except ImportError as e:
    sys.exit(f"Library Error: {e}. Install dulu: pip install ccxt pandas pandas_ta mplfinance requests numpy matplotlib")

# ==========================================
# 2. KONFIGURASI
# ==========================================
API_KEY = os.environ.get('BINANCE_API_KEY', 'fZwDMOfBL6rDU9jfUQox64fUAb2RSN48myxMPUGDAINYjmLdqJmUFhVRWLqlsX97')
API_SECRET = os.environ.get('BINANCE_API_SECRET', 'FmZNNbIOWIAddxVoLcNowLNW379E6gxyM85Bvy3QzlRMtK1eMApJp6vJtpGHWdWB')

# Telegram Config (Isi manual jika tidak pakai env var)
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', '8000712659:AAHltp77nGuakOzW9QMgQpVqnd5f1KgEsKA') 
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '-1003812500986')
 
# SETTING STRATEGI
TIMEFRAMES = ['1d', '4h', '1h', '15m']
VOL_MULTIPLIER = 2.0    
TOP_COIN_COUNT = 300    

# --- ANTI-BAN CONFIG ---
MAX_THREADS = 4         
DELAY_BETWEEN_TF = 0.5  
SCAN_COOLDOWN = 30      

OUTPUT_FOLDER = 'volume_safe_results'
os.makedirs(OUTPUT_FOLDER, exist_ok=True)
processed_signals = {} 

# ==========================================
# 3. KONEKSI EXCHANGE
# ==========================================
exchange = ccxt.binance({
    'apiKey': API_KEY, 'secret': API_SECRET,
    'options': {
        'defaultType': 'future',
        'adjustForTimeDifference': True
    },
    'enableRateLimit': True, 
})

# ==========================================
# 4. TELEGRAM & CHART
# ==========================================
def send_telegram_alert(symbol, data, image_path):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    
    icon = "🟢" if data['tipe'] == "BUY" else "🔴"
    
    caption = (
        f"🛡 <b>SAFE MODE: 5 INDIKATOR + MTF</b>\n"
        f"💎 <b>{symbol}</b>\n"
        f"──────────────────────\n"
        f"🇮🇩 <b>STRATEGI 5 INDIKATOR</b>\n"
        f"👴 <b>SUAMI:</b> {data['indo_suami']}\n"
        f"👶 <b>ADIK:</b> {data['indo_adik']}\n"
        f"💰 <b>CFM:</b> {data['indo_cfm']}\n"
        f"🛡 <b>SL (ATR):</b> {data['indo_atr_sl']}\n"
        f"──────────────────────\n"
        f"🌍 <b>LAYER ANALISIS</b>\n"
        f"1️⃣ <b>1D:</b> {data['layer_1d']}\n"
        f"2️⃣ <b>4H:</b> {data['layer_4h']}\n"
        f"3️⃣ <b>1H:</b> {data['layer_1h']}\n"
        f"4️⃣ <b>15M:</b> {data['layer_15m']}\n"
        f"──────────────────────\n"
        f"🎯 <b>Pattern:</b> {data['pattern']} {data['confirmation']}\n"
        f"🛠 <b>Signal:</b> {data['tipe']} {icon}\n"
        f"💰 <b>Price:</b> {data['price']}\n"
    )

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    try:
        with open(image_path, "rb") as img:
            requests.post(url, data={'chat_id': TELEGRAM_CHAT_ID, 'caption': caption, 'parse_mode': 'HTML'}, files={'photo': img}, timeout=20)
        
        # Hapus gambar setelah terkirim agar VPS tidak penuh
        try: os.remove(image_path)
        except: pass

    except Exception as e:
        print(f"❌ Gagal kirim TG: {e}")

def generate_chart(df, symbol, signal_info):
    try:
        # Bersihkan nama file dari karakter aneh
        safe_symbol = symbol.replace('/','-')
        filename = f"{OUTPUT_FOLDER}/{safe_symbol}_{signal_info['tipe']}_{int(time.time())}.png"
        
        # Pastikan index adalah Datetime
        plot_df = df.tail(80).copy()
        plot_df.index = pd.to_datetime(plot_df['timestamp'])
        
        # Style Chart
        # Gunakan style default jika nightclouds bermasalah
        my_style = mpf.make_mpf_style(base_mpf_style='charles', rc={'font.size': 8})
        
        adds = [
            mpf.make_addplot(plot_df['BB_Up'], color='green', width=0.5, alpha=0.3),
            mpf.make_addplot(plot_df['BB_Low'], color='green', width=0.5, alpha=0.3),
            mpf.make_addplot(plot_df['SUAMI'], color='red', width=1.5), 
            mpf.make_addplot(plot_df['ADIK'], color='cyan', width=1.0),
            mpf.make_addplot(plot_df['CFM'], panel=1, color='orange', ylabel='CFM'),
        ]
        
        title_text = f"{symbol} [15M] - {signal_info['tipe']} | SAFE MODE"
        
        # Generate Plot (Matplotlib Agg backend active)
        mpf.plot(plot_df, type='candle', style=my_style, addplot=adds, 
                 title=title_text, 
                 savefig=dict(fname=filename, bbox_inches='tight', dpi=100), 
                 volume=False, panel_ratios=(6, 2))
                 
        return filename
    except Exception as e:
        # Tampilkan error chart yang sebenarnya
        print(f"❌ Error Membuat Chart {symbol}: {e}")
        return None

# ==========================================
# 5. DATA FETCHING (SAFE FETCH)
# ==========================================
def fetch_data_safe(symbol, timeframe, limit=50, retries=3):
    for i in range(retries):
        try:
            bars = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            df = pd.DataFrame(bars, columns=['timestamp','open','high','low','close','volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            return df
        except ccxt.RateLimitExceeded:
            wait_time = (i + 1) * 2
            print(f"⚠️ Rate Limit! Cooling down {wait_time}s...")
            time.sleep(wait_time)
        except Exception as e:
            return None
    return None

def add_5_indicators(df):
    df['SUAMI'] = df.ta.sma(length=50)
    df['ADIK'] = df.ta.ema(length=10)
    df['ATR'] = df.ta.atr(length=14)
    df['OBV'] = df.ta.obv()
    df['CFM'] = df.ta.cmf(length=20)
    return df

def analyze_5_indicators_logic(df, tipe_signal):
    c = df.iloc[-2]; p = df.iloc[-3]
    suami_st = "BULLISH > MA50" if c['close'] > c['SUAMI'] else "BEARISH < MA50"
    
    adik_st = "NEUTRAL"
    if c['ADIK'] > c['SUAMI'] and p['ADIK'] <= p['SUAMI']: adik_st = "GOLDEN CROSS 🌟"
    elif c['ADIK'] < c['SUAMI'] and p['ADIK'] >= p['SUAMI']: adik_st = "DEATH CROSS 💀"
    elif c['ADIK'] > c['SUAMI']: adik_st = "UPTREND"
    else: adik_st = "DOWNTREND"
        
    cfm_st = "AKUMULASI 🟢" if c['CFM'] > 0 else "DISTRIBUSI 🔴"
    obv_st = "NAIK 📈" if c['OBV'] > p['OBV'] else "TURUN 📉"
    
    atr_val = c['ATR'] if pd.notnull(c['ATR']) else 0
    sl_price = c['close'] - (atr_val * 1.5) if tipe_signal == "BUY" else c['close'] + (atr_val * 1.5)
        
    return {
        'indo_suami': suami_st, 'indo_adik': adik_st, 'indo_cfm': cfm_st,
        'indo_obv': obv_st, 'indo_atr_sl': f"{sl_price:.4f}"
    }

def analyze_price_action(df):
    curr = df.iloc[-2]; prev = df.iloc[-3]
    curr_body = abs(curr['close'] - curr['open'])
    upper_wick = curr['high'] - max(curr['close'], curr['open'])
    lower_wick = min(curr['close'], curr['open']) - curr['low']
    
    pattern = "None"; signal = "NONE"
    
    if lower_wick > (curr_body * 2) and upper_wick < curr_body:
        pattern = "PIN BAR"; signal = "BUY"
    elif upper_wick > (curr_body * 2) and lower_wick < curr_body:
        pattern = "SHOOTING STAR"; signal = "SELL"
    elif curr['close'] > prev['open'] and curr['open'] < prev['close'] and curr['close'] > curr['open']:
        pattern = "BULL ENGULF"; signal = "BUY"
    elif curr['close'] < prev['open'] and curr['open'] > prev['close'] and curr['close'] < curr['open']:
        pattern = "BEAR ENGULF"; signal = "SELL"
        
    return pattern, signal

# ==========================================
# 6. WORKER: MULTI-LAYER (THROTTLED)
# ==========================================
def worker_multi_layer(symbol):
    try:
        # LAYER 1: 1D
        df_1d = fetch_data_safe(symbol, '1d', limit=60)
        if df_1d is None: return None
        
        ema50_1d = ta.ema(df_1d['close'], length=50).iloc[-1]
        bias_1d = "BULLISH" if df_1d['close'].iloc[-1] > ema50_1d else "BEARISH"
        time.sleep(DELAY_BETWEEN_TF)

        # LAYER 2: 4H
        df_4h = fetch_data_safe(symbol, '4h', limit=50)
        if df_4h is None: return None
        
        rsi_4h = ta.rsi(df_4h['close'], length=14).iloc[-1]
        if bias_1d == "BULLISH" and rsi_4h > 75: return None 
        if bias_1d == "BEARISH" and rsi_4h < 25: return None
        status_4h = f"RSI {rsi_4h:.0f}"
        time.sleep(DELAY_BETWEEN_TF)

        # LAYER 3: 1H
        df_1h = fetch_data_safe(symbol, '1h', limit=50)
        if df_1h is None: return None
        bb_1h = df_1h.ta.bbands(length=20, std=2)
        status_1h = "OK" if (bias_1d=="BULLISH" and df_1h['close'].iloc[-1] > bb_1h.iloc[-1, 1]) or \
                            (bias_1d=="BEARISH" and df_1h['close'].iloc[-1] < bb_1h.iloc[-1, 1]) else "Weak"
        if status_1h == "Weak": return None
        time.sleep(DELAY_BETWEEN_TF)

        # LAYER 4: 15M (ENTRY)
        df_15m = fetch_data_safe(symbol, '15m', limit=100)
        if df_15m is None: return None
        
        df_15m = add_5_indicators(df_15m)
        df_15m.ta.bbands(length=20, std=2, append=True)
        
        curr_vol = df_15m['volume'].iloc[-2]
        avg_vol = df_15m['volume'].iloc[-8:-2].mean()
        spike_ratio = curr_vol / avg_vol if avg_vol > 0 else 0
        
        if spike_ratio < VOL_MULTIPLIER: return None
        
        pattern, pa_signal = analyze_price_action(df_15m)

        is_valid = False
        tipe_signal = "NONE"
        c = df_15m.iloc[-2]
        
        if bias_1d == "BULLISH":
            if c['close'] > c['SUAMI']: 
                if pa_signal == "BUY" or c['ADIK'] > c['SUAMI']: 
                    is_valid = True; tipe_signal = "BUY"
        elif bias_1d == "BEARISH":
            if c['close'] < c['SUAMI']: 
                if pa_signal == "SELL" or c['ADIK'] < c['SUAMI']: 
                    is_valid = True; tipe_signal = "SELL"

        if is_valid:
            indo_data = analyze_5_indicators_logic(df_15m, tipe_signal)
            res = {
                'symbol': symbol, 'time': df_15m['timestamp'].iloc[-2],
                'price': df_15m['close'].iloc[-1], 'tipe': tipe_signal,
                'layer_1d': bias_1d, 'layer_4h': status_4h, 'layer_1h': status_1h,
                'layer_15m': f"Vol {spike_ratio:.1f}x",
                'pattern': pattern, 'confirmation': "WAITING", 'spike_ratio': spike_ratio,
                'df': df_15m
            }
            res.update(indo_data)
            return res

    except Exception: return None
    return None

# ==========================================
# 7. MAIN LOOP
# ==========================================
def main():
    print(f"=== BOT ANTI-BAN (SAFE MODE + CHART FIX) ===")
    print(f"Threads: {MAX_THREADS} | Delay TF: {DELAY_BETWEEN_TF}s")
    
    global processed_signals

    while True:
        try:
            exchange.load_markets()
            tickers = exchange.fetch_tickers()
            symbols = sorted([t for t in tickers if '/USDT' in t and 'UP/' not in t], 
                             key=lambda x: tickers[x]['quoteVolume'], reverse=True)[:TOP_COIN_COUNT]
            random.shuffle(symbols) 

            sys.stdout.write(f"\n[{datetime.now().strftime('%H:%M:%S')}] 🛡 Safe Scanning {len(symbols)} coins...\n")
            
            with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_THREADS) as executor:
                futures = {executor.submit(worker_multi_layer, sym): sym for sym in symbols}
                completed = 0
                for future in concurrent.futures.as_completed(futures):
                    completed += 1
                    res = future.result()
                    
                    if completed % 20 == 0:
                        sys.stdout.write(f"\rProgress: {completed}/{len(symbols)}")
                        sys.stdout.flush()

                    if res:
                        sym = res['symbol']
                        if processed_signals.get(sym) != res['time']:
                            processed_signals[sym] = res['time']
                            print(f"\n🔥 {sym} [{res['tipe']}]")
                            img = generate_chart(res['df'], sym, res)
                            
                            # Log jika gambar gagal dibuat
                            if img is None:
                                print(f"⚠️ Gambar chart {sym} gagal dibuat.")
                            
                            send_telegram_alert(sym, res, img)
            
            print(f"\n✅ Scan Selesai. Istirahat {SCAN_COOLDOWN} detik...")
            time.sleep(SCAN_COOLDOWN) 

        except KeyboardInterrupt: break
        except Exception as e:
            print(f"Error Utama: {e}"); time.sleep(60)

if __name__ == "__main__":
    main()



