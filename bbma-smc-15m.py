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
API_KEY = os.environ.get('BINANCE_API_KEY', 'IbZV3RoXl5ErnYaNT2tLwT3OtvNK9Gy9vgR11QIaL36or8WCZT6TQAItaTXZ4IOc')
API_SECRET = os.environ.get('BINANCE_API_SECRET', 'ks4gpwvQkVVWxM6AKkxkcjD1x9ZO1vtyTwMYD5sGJyW8oraJ1Z1IM6kfQyeOSOEB')

# Telegram Config
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', '8000712659:AAHltp77nGuakOzW9QMgQpVqnd5f1KgEsKA') 
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '-1003842052901')

# SETTING STRATEGI
TIMEFRAME = '15m'       
VOL_MULTIPLIER = 2.0    # Volume minimal 2x rata-rata 24 jam
LIMIT = 200             
TOP_COIN_COUNT = 300    
MAX_THREADS = 15        

OUTPUT_FOLDER = 'volume_15m_results'
os.makedirs(OUTPUT_FOLDER, exist_ok=True)
processed_signals = {} 

# ==========================================
# 3. KONEKSI EXCHANGE
# ==========================================
exchange = ccxt.binance({
    'apiKey': API_KEY, 'secret': API_SECRET,
    'options': {'defaultType': 'spot'},
    'enableRateLimit': True, 
})

# ==========================================
# 4. TELEGRAM & CHART
# ==========================================
def send_telegram_alert(symbol, data, image_path):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    
    icon = "🟢" if data['tipe'] == "BUY" else "🔴"
    spike_pct = (data['spike_ratio'] * 100) - 100
    
    # Format pesan chart pattern & SMC
    pattern_txt = data.get('pattern', '-')
    smc_txt = data.get('smc_context', '-')
    
    caption = (
        f"🐋 <b>BBMA + SMC VOLUME ALERT (15M)</b>\n"
        f"💎 <b>{symbol}</b>\n"
        f"──────────────────────\n"
        f"📊 <b>Vol Spike:</b> {data['spike_ratio']:.2f}x\n"
        f"🔥 <b>Lonjakan:</b> +{spike_pct:.0f}%\n"
        f"──────────────────────\n"
        f"🛠 <b>Setup BBMA:</b> {data['signal']} {icon}\n"
        f"🧠 <b>SMC Vol:</b> {smc_txt}\n"
        f"📐 <b>Pola:</b> {pattern_txt}\n"
        f"──────────────────────\n"
        f"🏷 <b>Tipe:</b> {data['tipe']}\n"
        f"💰 <b>Harga:</b> {data['price']}\n\n"
        f"📝 <b>Analisa:</b>\n{data['explanation']}\n"
    )

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    try:
        with open(image_path, "rb") as img:
            requests.post(url, data={'chat_id': TELEGRAM_CHAT_ID, 'caption': caption, 'parse_mode': 'HTML'}, files={'photo': img}, timeout=20)
    except Exception as e:
        print(f"Gagal kirim TG: {e}")

def generate_chart(df, symbol, signal_info):
    try:
        smc_info = signal_info.get('smc_context', '')
        title_text = f"{symbol} [15M] - {signal_info['signal']}"
        if "DIVERGENCE" in smc_info:
            title_text += f" | SMC: {smc_info}"

        filename = f"{OUTPUT_FOLDER}/{symbol.replace('/','-')}_{signal_info['signal']}.png"
        plot_df = df.tail(80).copy()
        plot_df.set_index('timestamp', inplace=True)
        
        style = mpf.make_mpf_style(base_mpf_style='nightclouds', rc={'font.size': 8})
        adds = [
            mpf.make_addplot(plot_df['BB_Up'], color='green', width=1),
            mpf.make_addplot(plot_df['BB_Mid'], color='orange', width=1, linestyle='--'),
            mpf.make_addplot(plot_df['BB_Low'], color='green', width=1),
            mpf.make_addplot(plot_df['MA5_Hi'], color='cyan', width=0.7),
            mpf.make_addplot(plot_df['MA5_Lo'], color='magenta', width=0.7),
            mpf.make_addplot(plot_df['MA10_Hi'], color='yellow', width=0.7),
            mpf.make_addplot(plot_df['MA10_Lo'], color='white', width=0.7),
        ]
        
        mpf.plot(plot_df, type='candle', style=style, addplot=adds, 
                 title=title_text, 
                 savefig=dict(fname=filename, bbox_inches='tight'), volume=True)
    
        return filename
    except Exception as e:
        print(f"Chart Error: {e}")
        return None

# ==========================================
# 5. DATA ENGINE
# ==========================================
def get_top_symbols(limit=300):
    try:
        exchange.load_markets()
        tickers = exchange.fetch_tickers()
        valid_tickers = [t for t in tickers.values() if '/USDT' in t['symbol'] and 'USDC' not in t['symbol'] and 'UP/' not in t['symbol'] and 'DOWN/' not in t['symbol']]
        sorted_tickers = sorted(valid_tickers, key=lambda x: x['quoteVolume'] if x['quoteVolume'] else 0, reverse=True)
        return [t['symbol'] for t in sorted_tickers[:limit]]
    except: return []

def fetch_ohlcv(symbol):
    try:
        bars = exchange.fetch_ohlcv(symbol, TIMEFRAME, limit=LIMIT)
        df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        return df
    except: return None

def add_indicators(df):
    # Bollinger Bands
    bb = df.ta.bbands(length=20, std=2)
    df['BB_Up'] = bb.iloc[:, 2]
    df['BB_Mid'] = bb.iloc[:, 1]
    df['BB_Low'] = bb.iloc[:, 0]
    
    # MA 5 & 10
    df['MA5_Hi'] = df['high'].rolling(5).mean()
    df['MA5_Lo'] = df['low'].rolling(5).mean()
    df['MA10_Hi'] = df['high'].rolling(10).mean()
    df['MA10_Lo'] = df['low'].rolling(10).mean()
    
    # EMA 50 untuk Trend
    df['EMA_50'] = df.ta.ema(length=50)
    
    return df

# ==========================================
# 6. LOGIKA ANALISIS (BBMA, PATTERN, SMC)
# ==========================================

# --- A. SMC VOLUME DIVERGENCE ---
def analyze_smc_divergence(df):
    """
    Menganalisa Divergensi Volume:
    1. Bearish Divergence: Harga NAIK, Volume TURUN (Kelelahan Buyer)
    2. Bullish Divergence: Harga TURUN, Volume TURUN (Kelelahan Seller)
    Menggunakan Regresi Linear (Slope) pada 10-15 candle terakhir sebelum spike.
    """
    if df is None or len(df) < 20: return "Data Kurang"

    # Ambil data sebelum candle terakhir (karena candle terakhir adalah spike)
    # Kita cek tren volume pada candle -15 sampai -2
    subset = df.iloc[-15:-2]
    
    closes = subset['close'].values
    volumes = subset['volume'].values
    x = np.arange(len(closes))
    
    # Hitung Kemiringan (Slope) Harga dan Volume
    slope_price, _ = np.polyfit(x, closes, 1)
    slope_vol, _ = np.polyfit(x, volumes, 1)
    
    # Normalisasi threshold slope agar tidak terlalu sensitif pada sideways
    price_threshold = closes[0] * 0.0002 # 0.02% perubahan dianggap tren

    result = "NORMAL"

    # 1. Bearish Divergence (Weak Rally / Exhaustion)
    # Harga Naik (Slope > 0) TAPI Volume Turun (Slope < 0)
    if slope_price > price_threshold and slope_vol < 0:
        result = "BEARISH DIVERGENCE (Weak Buyers ⚠️)"

    # 2. Bullish Divergence (Weak Dump / Absorbtion)
    # Harga Turun (Slope < 0) TAPI Volume Turun (Slope < 0)
    elif slope_price < -price_threshold and slope_vol < 0:
        result = "BULLISH DIVERGENCE (Weak Sellers ⚠️)"
        
    return result

# --- B. CHART PATTERN ---
def analyze_chart_pattern(df):
    if df is None or len(df) < 50: return None
    subset = df.iloc[-50:]
    closes = subset['close'].values
    highs = subset['high'].values
    lows = subset['low'].values
    pattern_found = []
    
    # Deteksi Double Top/Bottom
    peaks_idx = subset['high'].rolling(window=5, center=True).max() == subset['high']
    peaks = subset[peaks_idx]
    troughs_idx = subset['low'].rolling(window=5, center=True).min() == subset['low']
    troughs = subset[troughs_idx]
    
    if len(peaks) >= 2:
        if abs(peaks.iloc[-1]['high'] - peaks.iloc[-2]['high']) / peaks.iloc[-2]['high'] < 0.005:
             if (peaks.iloc[-1].name - peaks.iloc[-2].name) > 5:
                 pattern_found.append("Double Top")
    if len(troughs) >= 2:
        if abs(troughs.iloc[-1]['low'] - troughs.iloc[-2]['low']) / troughs.iloc[-2]['low'] < 0.005:
            if (troughs.iloc[-1].name - troughs.iloc[-2].name) > 5:
                pattern_found.append("Double Bottom")

    # Deteksi Wedge
    y_high = highs[-20:]; y_low = lows[-20:]; x = np.arange(len(y_high))
    slope_high, _ = np.polyfit(x, y_high, 1)
    slope_low, _ = np.polyfit(x, y_low, 1)
    flat_threshold = 0.0005 * closes[-1] 

    if slope_high < -flat_threshold and slope_low < 0 and slope_high < slope_low:
        pattern_found.append("Falling Wedge")
    elif slope_high > 0 and slope_low > flat_threshold and slope_low > slope_high:
        pattern_found.append("Rising Wedge")
    elif (closes[-10] - closes[-30]) > 0 and abs(slope_high) < flat_threshold and abs(slope_low) < flat_threshold:
        pattern_found.append("Bullish Rectangle")
    elif (closes[-10] - closes[-30]) < 0 and abs(slope_high) < flat_threshold and abs(slope_low) < flat_threshold:
        pattern_found.append("Bearish Rectangle")

    return ", ".join(pattern_found) if pattern_found else None

# --- C. BBMA OMA ALLY ---
def analyze_bbma_setup(df):
    if df is None or len(df) < 30: return None
    c = df.iloc[-2]; prev = df.iloc[-3]
    recent_data = df.iloc[-15:-2] 
    res = None

    # Logic BBMA (Extreme, MHV, CSAK, Re-Entry)
    extreme_buy = c['MA5_Lo'] < c['BB_Low']
    extreme_sell = c['MA5_Hi'] > c['BB_Up']
    
    mhv_buy = (prev['MA5_Lo'] < prev['BB_Low']) and (c['close'] > c['BB_Low']) and (c['close'] < c['BB_Mid'])
    mhv_sell = (prev['MA5_Hi'] > prev['BB_Up']) and (c['close'] < c['BB_Up']) and (c['close'] > c['BB_Mid'])

    csak_buy = (c['close'] > c['BB_Mid']) and (prev['close'] < c['BB_Mid']) and (c['close'] > c['MA5_Hi']) and (c['close'] > c['MA10_Hi'])
    csak_sell = (c['close'] < c['BB_Mid']) and (prev['close'] > c['BB_Mid']) and (c['close'] < c['MA5_Lo']) and (c['close'] < c['MA10_Lo'])

    had_momentum_buy = any(recent_data['close'] > recent_data['BB_Up'])
    had_momentum_sell = any(recent_data['close'] < recent_data['BB_Low'])
    reentry_buy = had_momentum_buy and (c['low'] <= c['MA5_Lo'] or c['low'] <= c['MA10_Lo']) and (c['close'] > c['MA5_Lo'])
    reentry_sell = had_momentum_sell and (c['high'] >= c['MA5_Hi'] or c['high'] >= c['MA10_Hi']) and (c['close'] < c['MA5_Hi'])

    if extreme_buy: res = {"signal": "EXTREME BUY", "tipe": "BUY", "explanation": "MA5 Low keluar BB Low (Reversal)."}
    elif extreme_sell: res = {"signal": "EXTREME SELL", "tipe": "SELL", "explanation": "MA5 High keluar BB Top (Reversal)."}
    elif mhv_buy: res = {"signal": "MHV BUY", "tipe": "BUY", "explanation": "Gagal tembus BB Low setelah Extreme."}
    elif mhv_sell: res = {"signal": "MHV SELL", "tipe": "SELL", "explanation": "Gagal tembus BB Top setelah Extreme."}
    elif csak_buy: res = {"signal": "CSAK BUY", "tipe": "BUY", "explanation": "Break Mid BB + MA5/10 High (Kukuh)."}
    elif csak_sell: res = {"signal": "CSAK SELL", "tipe": "SELL", "explanation": "Break Mid BB + MA5/10 Low (Kukuh)."}
    elif reentry_buy: res = {"signal": "RE-ENTRY BUY", "tipe": "BUY", "explanation": "Koreksi ke MA5/10 Low setelah Momentum."}
    elif reentry_sell: res = {"signal": "RE-ENTRY SELL", "tipe": "SELL", "explanation": "Koreksi ke MA5/10 High setelah Momentum."}

    if res:
        res['price'] = c['close']; res['time'] = c['timestamp']
        return res
    return None

def analyze_volume_anomaly(df):
    if df is None or len(df) < 100: return 0
    current_vol = df['volume'].iloc[-2]
    avg_vol_24h = df['volume'].iloc[-98:-2].mean()
    return current_vol / avg_vol_24h if avg_vol_24h > 0 else 0

# ==========================================
# 7. WORKER SCAN
# ==========================================
def worker_scan(symbol):
    try:
        df = fetch_ohlcv(symbol)
        if df is None: return None

        # 1. Cek Volume Spike (Pintu Masuk Utama)
        spike_ratio = analyze_volume_anomaly(df)
        if spike_ratio < VOL_MULTIPLIER:
            return None

        df = add_indicators(df)
        
        # 2. Cek BBMA Setup
        res = analyze_bbma_setup(df)
        
        if res:
            # 3. Analisa SMC Volume Divergence
            smc_context = analyze_smc_divergence(df)
            
            # 4. Analisa Chart Pattern
            pattern_data = analyze_chart_pattern(df)
            
            res['symbol'] = symbol
            res['spike_ratio'] = spike_ratio
            res['df'] = df
            res['smc_context'] = smc_context
            res['pattern'] = pattern_data if pattern_data else "-"
            
            return res
    except: pass
    return None

# ==========================================
# 8. MAIN LOOP
# ==========================================
def main():
    print(f"🚀 BBMA + SMC VOLUME BOT STARTED")
    print(f"Filter: Vol Spike > {VOL_MULTIPLIER}x | SMC Divergence Check | TF: {TIMEFRAME}")
    
    global processed_signals

    while True:
        try:
            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Scan Market & SMC Logic...")
            symbols = get_top_symbols(TOP_COIN_COUNT)
            alerts_queue = []
            
            completed = 0
            start_t = time.time()
            
            with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_THREADS) as executor:
                futures = {executor.submit(worker_scan, sym): sym for sym in symbols}
                for future in concurrent.futures.as_completed(futures):
                    res = future.result()
                    if res: alerts_queue.append(res)
                    completed += 1
                    if completed % 30 == 0:
                        sys.stdout.write(f"\rProgress: {completed} koin...")
                        sys.stdout.flush()
            
            duration = time.time() - start_t
            print(f"\n✅ Selesai ({duration:.1f}s). Ditemukan: {len(alerts_queue)} setup.")

            alerts_queue.sort(key=lambda x: x['spike_ratio'], reverse=True)

            for alert in alerts_queue:
                sym = alert['symbol']
                sig_key = f"{sym}_{alert['signal']}"
                if processed_signals.get(sig_key) != alert['time']:
                    processed_signals[sig_key] = alert['time']
                    
                    smc_tag = f"[SMC: {alert['smc_context']}]" if "DIVERGENCE" in alert['smc_context'] else ""
                    print(f"🔥 {sym} -> {alert['signal']} {smc_tag}")
                    
                    img = generate_chart(alert['df'], sym, alert)
                    if img: 
                        send_telegram_alert(sym, alert, img)
            
            print("⏳ Menunggu 30 detik...")
            time.sleep(30)

        except KeyboardInterrupt: 
            break
        except Exception as e:
            print(f"Error: {e}")
            time.sleep(10)

if __name__ == "__main__":
    main()
