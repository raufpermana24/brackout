import sys
import os
import time
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
except ImportError as e:
    sys.exit(f"Library Error: {e}. Install dulu: pip install ccxt pandas pandas_ta mplfinance requests numpy")

# ==========================================
# 2. KONFIGURASI
# ==========================================
API_KEY = os.environ.get('BINANCE_API_KEY', 'fZwDMOfBL6rDU9jfUQox64fUAb2RSN48myxMPUGDAINYjmLdqJmUFhVRWLqlsX97') 
API_SECRET = os.environ.get('BINANCE_API_SECRET', 'FmZNNbIOWIAddxVoLcNowLNW379E6gxyM85Bvy3QzlRMtK1eMApJp6vJtpGHWdWB')

# Telegram Config
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', '8000712659:AAHltp77nGuakOzW9QMgQpVqnd5f1KgEsKA') 
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '-1003812500986')
 
# SETTING STRATEGI
TIMEFRAMES = ['1d', '4h', '1h', '15m']
VOL_MULTIPLIER = 2.0    
TOP_COIN_COUNT = 300    
MAX_THREADS = 20        

OUTPUT_FOLDER = 'volume_indo_results'
os.makedirs(OUTPUT_FOLDER, exist_ok=True)
processed_signals = {} 

# ==========================================
# 3. KONEKSI EXCHANGE
# ==========================================
exchange = ccxt.binance({
    'apiKey': API_KEY, 'secret': API_SECRET,
    'options': {'defaultType': 'future'},
    'enableRateLimit': True, 
})

# ==========================================
# 4. TELEGRAM & CHART
# ==========================================
def send_telegram_alert(symbol, data, image_path):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    
    icon = "🟢" if data['tipe'] == "BUY" else "🔴"
    
    # Format Text Telegram Lengkap
    caption = (
        f"🚀 <b>MULTI-LAYER + 5 INDIKATOR ALERT</b>\n"
        f"💎 <b>{symbol}</b>\n"
        f"──────────────────────\n"
        f"🇮🇩 <b>STRATEGI 5 INDIKATOR (15M)</b>\n"
        f"👴 <b>SUAMI (Trend):</b> {data['indo_suami']}\n"
        f"👶 <b>ADIK (Signal):</b> {data['indo_adik']}\n"
        f"💰 <b>Arus Uang (CFM):</b> {data['indo_cfm']}\n"
        f"📊 <b>Vol (OBV):</b> {data['indo_obv']}\n"
        f"🛡 <b>ATR Stop Loss:</b> {data['indo_atr_sl']}\n"
        f"──────────────────────\n"
        f"🌍 <b>ANALISA MULTI-LAYER</b>\n"
        f"1️⃣ <b>1D:</b> {data['layer_1d']}\n"
        f"2️⃣ <b>4H:</b> {data['layer_4h']}\n"
        f"3️⃣ <b>1H:</b> {data['layer_1h']}\n"
        f"4️⃣ <b>15M:</b> {data['layer_15m']}\n"
        f"──────────────────────\n"
        f"🎯 <b>Price Action:</b> {data['pattern']} {data['confirmation']}\n"
        f"🛠 <b>Action:</b> {data['tipe']} NOW {icon}\n"
        f"💰 <b>Price:</b> {data['price']}\n"
    )

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    try:
        with open(image_path, "rb") as img:
            requests.post(url, data={'chat_id': TELEGRAM_CHAT_ID, 'caption': caption, 'parse_mode': 'HTML'}, files={'photo': img}, timeout=20)
    except Exception as e:
        print(f"Gagal kirim TG: {e}")

def generate_chart(df, symbol, signal_info):
    try:
        filename = f"{OUTPUT_FOLDER}/{symbol.replace('/','-')}_{signal_info['tipe']}.png"
        plot_df = df.tail(80).set_index('timestamp')
        
        style = mpf.make_mpf_style(base_mpf_style='nightclouds', rc={'font.size': 8})
        
        # Plotting SUAMI (SMA50) & ADIK (EMA10)
        adds = [
            mpf.make_addplot(plot_df['BB_Up'], color='green', width=0.5, alpha=0.3),
            mpf.make_addplot(plot_df['BB_Low'], color='green', width=0.5, alpha=0.3),
            # SUAMI = Merah Tebal
            mpf.make_addplot(plot_df['SUAMI'], color='red', width=1.5), 
            # ADIK = Kuning/Biru Cepat
            mpf.make_addplot(plot_df['ADIK'], color='cyan', width=1.0),
            # CMF di Panel Bawah
            mpf.make_addplot(plot_df['CFM'], panel=1, color='orange', ylabel='CFM'),
        ]
        
        title_text = f"{symbol} [15M] - {signal_info['tipe']} | SUAMI: {signal_info['indo_suami']}"
        
        mpf.plot(plot_df, type='candle', style=style, addplot=adds, 
                 title=title_text, savefig=dict(fname=filename, bbox_inches='tight'), 
                 volume=False, panel_ratios=(6, 2))
        return filename
    except: return None

# ==========================================
# 5. LOGIKA DATA & 5 INDIKATOR
# ==========================================
def fetch_data(symbol, timeframe, limit=50):
    try:
        bars = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        df = pd.DataFrame(bars, columns=['timestamp','open','high','low','close','volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        return df
    except: return None

def add_5_indicators(df):
    """Menambahkan 5 Indikator Lokal: ATR, OBV, CFM, SUAMI, ADIK"""
    # 1. SUAMI (SMA 50) - Tren Jangka Menengah
    df['SUAMI'] = df.ta.sma(length=50)
    
    # 2. ADIK (EMA 10) - Tren Jangka Pendek
    df['ADIK'] = df.ta.ema(length=10)
    
    # 3. ATR (Average True Range) - Volatilitas (Stop Loss)
    df['ATR'] = df.ta.atr(length=14)
    
    # 4. OBV (On Balance Volume)
    df['OBV'] = df.ta.obv()
    
    # 5. CFM (Chaikin Money Flow) - Akumulasi/Distribusi
    df['CFM'] = df.ta.cmf(length=20)
    
    return df

def analyze_5_indicators_logic(df, tipe_signal):
    """Menganalisa status 5 indikator"""
    c = df.iloc[-2] # Candle closed
    p = df.iloc[-3] # Previous candle
    
    # Analisa SUAMI
    suami_st = "BULLISH (Price > MA50)" if c['close'] > c['SUAMI'] else "BEARISH (Price < MA50)"
    
    # Analisa ADIK (Crossing)
    adik_st = "NEUTRAL"
    if c['ADIK'] > c['SUAMI'] and p['ADIK'] <= p['SUAMI']:
        adik_st = "GOLDEN CROSS 🌟"
    elif c['ADIK'] < c['SUAMI'] and p['ADIK'] >= p['SUAMI']:
        adik_st = "DEATH CROSS 💀"
    elif c['ADIK'] > c['SUAMI']:
        adik_st = "UPTREND STRONG"
    else:
        adik_st = "DOWNTREND STRONG"
        
    # Analisa CFM (Money Flow)
    cfm_st = "AKUMULASI (Masuk) 🟢" if c['CFM'] > 0 else "DISTRIBUSI (Keluar) 🔴"
    
    # Analisa OBV
    obv_st = "NAIK 📈" if c['OBV'] > p['OBV'] else "TURUN 📉"
    
    # Hitung ATR Stop Loss
    atr_val = c['ATR']
    if tipe_signal == "BUY":
        sl_price = c['close'] - (atr_val * 1.5) # SL di bawah harga (1.5x ATR)
    else:
        sl_price = c['close'] + (atr_val * 1.5) # SL di atas harga
        
    return {
        'indo_suami': suami_st,
        'indo_adik': adik_st,
        'indo_cfm': cfm_st,
        'indo_obv': obv_st,
        'indo_atr_sl': f"{sl_price:.5f}"
    }

def analyze_price_action(df):
    curr = df.iloc[-2]; prev = df.iloc[-3]
    curr_body = abs(curr['close'] - curr['open'])
    upper_wick = curr['high'] - max(curr['close'], curr['open'])
    lower_wick = min(curr['close'], curr['open']) - curr['low']
    
    pattern = "None"; signal = "NONE"
    
    if lower_wick > (curr_body * 2) and upper_wick < curr_body:
        pattern = "PIN BAR (Hammer)"; signal = "BUY"
    elif upper_wick > (curr_body * 2) and lower_wick < curr_body:
        pattern = "SHOOTING STAR"; signal = "SELL"
    elif curr['close'] > prev['open'] and curr['open'] < prev['close'] and curr['close'] > curr['open']:
        pattern = "BULLISH ENGULFING"; signal = "BUY"
    elif curr['close'] < prev['open'] and curr['open'] > prev['close'] and curr['close'] < curr['open']:
        pattern = "BEARISH ENGULFING"; signal = "SELL"
        
    return pattern, signal

# ==========================================
# 6. WORKER: MULTI-LAYER + 5 INDIKATOR
# ==========================================
def worker_multi_layer(symbol):
    try:
        # LAYER 1: 1D (TREND)
        df_1d = fetch_data(symbol, '1d', limit=60)
        if df_1d is None: return None
        ema50_1d = ta.ema(df_1d['close'], length=50).iloc[-1]
        bias_1d = "BULLISH" if df_1d['close'].iloc[-1] > ema50_1d else "BEARISH"
        
        # LAYER 2: 4H (MOMENTUM)
        df_4h = fetch_data(symbol, '4h', limit=50)
        if df_4h is None: return None
        rsi_4h = ta.rsi(df_4h['close'], length=14).iloc[-1]
        if bias_1d == "BULLISH" and rsi_4h > 75: return None 
        if bias_1d == "BEARISH" and rsi_4h < 25: return None
        status_4h = f"RSI {rsi_4h:.1f} (OK)"

        # LAYER 3: 1H (STRUCTURE)
        df_1h = fetch_data(symbol, '1h', limit=50)
        if df_1h is None: return None
        bb_1h = df_1h.ta.bbands(length=20, std=2)
        status_1h = "Above Mid BB" if df_1h['close'].iloc[-1] > bb_1h.iloc[-1, 1] else "Below Mid BB"

        # LAYER 4: 15M (ENTRY & 5 INDIKATOR)
        df_15m = fetch_data(symbol, '15m', limit=100)
        if df_15m is None: return None
        
        # Hitung Indikator Wajib
        df_15m = add_5_indicators(df_15m)
        df_15m.ta.bbands(length=20, std=2, append=True)
        
        # Cek Volatilitas Volume
        curr_vol = df_15m['volume'].iloc[-2]
        avg_vol = df_15m['volume'].iloc[-8:-2].mean()
        spike_ratio = curr_vol / avg_vol if avg_vol > 0 else 0
        if spike_ratio < VOL_MULTIPLIER: return None
        
        # Cek Price Action
        pattern, pa_signal = analyze_price_action(df_15m)

        # KEPUTUSAN FINAL
        is_valid = False
        tipe_signal = "NONE"
        
        # Logika Gabungan: Bias 1D + (PA Signal ATAU SUAMI/ADIK Signal)
        c = df_15m.iloc[-2]
        
        if bias_1d == "BULLISH":
            # Syarat Buy: Harga > SUAMI DAN (Ada Price Action Buy ATAU ADIK > SUAMI)
            if c['close'] > c['SUAMI']:
                if pa_signal == "BUY" or c['ADIK'] > c['SUAMI']:
                    is_valid = True
                    tipe_signal = "BUY"
                    
        elif bias_1d == "BEARISH":
            # Syarat Sell: Harga < SUAMI DAN (Ada Price Action Sell ATAU ADIK < SUAMI)
            if c['close'] < c['SUAMI']:
                if pa_signal == "SELL" or c['ADIK'] < c['SUAMI']:
                    is_valid = True
                    tipe_signal = "SELL"

        if is_valid:
            # Analisa detail 5 Indikator untuk laporan
            indo_data = analyze_5_indicators_logic(df_15m, tipe_signal)
            
            curr_price = df_15m['close'].iloc[-1]
            conf_status = "RUNNING ✅"
            
            res = {
                'symbol': symbol,
                'time': df_15m['timestamp'].iloc[-2],
                'price': curr_price,
                'tipe': tipe_signal,
                'layer_1d': f"{bias_1d}",
                'layer_4h': status_4h,
                'layer_1h': status_1h,
                'layer_15m': f"Vol {spike_ratio:.1f}x",
                'pattern': pattern,
                'confirmation': conf_status,
                'spike_ratio': spike_ratio,
                'df': df_15m
            }
            # Gabungkan dictionary
            res.update(indo_data)
            return res

    except Exception as e: return None
    return None

# ==========================================
# 7. MAIN LOOP
# ==========================================
def main():
    print(f"=== BOT 5 INDIKATOR + MULTI LAYER ===")
    print("Indikator: SUAMI (SMA50), ADIK (EMA10), ATR, OBV, CFM")
    print("Mode: Real-Time Scanning")
    
    global processed_signals

    while True:
        try:
            exchange.load_markets()
            tickers = exchange.fetch_tickers()
            symbols = sorted([t for t in tickers if '/USDT' in t and 'UP/' not in t], 
                             key=lambda x: tickers[x]['quoteVolume'], reverse=True)[:TOP_COIN_COUNT]
            
            sys.stdout.write(f"\n[{datetime.now().strftime('%H:%M:%S')}] 🚀 Scanning {len(symbols)} coins...\n")
            
            found = 0
            with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_THREADS) as executor:
                futures = {executor.submit(worker_multi_layer, sym): sym for sym in symbols}
                completed = 0
                for future in concurrent.futures.as_completed(futures):
                    completed += 1
                    res = future.result()
                    if completed % 50 == 0:
                        sys.stdout.write(f"\rProgress: {completed}/{len(symbols)}")
                        sys.stdout.flush()

                    if res:
                        sym = res['symbol']
                        if processed_signals.get(sym) != res['time']:
                            processed_signals[sym] = res['time']
                            found += 1
                            print(f"\n🔥 {sym} [{res['tipe']}] SUAMI:{res['indo_suami']} | ADIK:{res['indo_adik']}")
                            img = generate_chart(res['df'], sym, res)
                            if img: send_telegram_alert(sym, res, img)
            
            print(f"\n✅ Scan Selesai. Ditemukan: {found}. Re-scanning in 10s...")
            time.sleep(10) 

        except KeyboardInterrupt: break
        except Exception as e:
            print(f"Error: {e}"); time.sleep(10)

if __name__ == "__main__":
    main()


