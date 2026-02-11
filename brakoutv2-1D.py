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

# Telegram Config (Isi manual jika tidak pakai env var)
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', '8000712659:AAHltp77nGuakOzW9QMgQpVqnd5f1KgEsKA') 
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '-1003726025593')

 
# SETTING STRATEGI MULTI-TIMEFRAME
HTF = '1d'              # Timeframe Tinggi untuk Filter Tren Besar
LTF = '1h'              # Timeframe Rendah untuk Eksekusi/Entry
VOL_MULTIPLIER = 2.0    
LIMIT = 150             
TOP_COIN_COUNT = 300    
MAX_THREADS = 15        # Ditingkatkan untuk MTF scan

# FITUR FILTER
CARI_BB_DATAR = True    

OUTPUT_FOLDER = 'volume_mtf_results'
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
    bb_icon = "🤐" if "SQUEEZE" in data['bb_status'] else "💥"
    
    caption = (
        f"💎 <b>MTF ALERT: {symbol}</b>\n"
        f"──────────────────────\n"
        f"🌍 <b>HTF Trend ({HTF}):</b> {data['htf_trend']}\n"
        f"🏛 <b>LTF Structure ({LTF}):</b> {data['structure']}\n"
        f"──────────────────────\n"
        f"📊 <b>Vol Spike:</b> {data['spike_ratio']:.2f}x\n"
        f"📏 <b>BB:</b> {data['bb_status']} {bb_icon}\n"
        f"🌊 <b>Div:</b> {data['divergence']}\n"
        f"──────────────────────\n"
        f"🛠 <b>Signal:</b> {data['signal']} {icon}\n"
        f"💰 <b>Entry Price:</b> {data['price']}\n"
        f"📝 <b>Note:</b> Tren HTF & LTF Selaras ✅\n"
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
        adds = [
            mpf.make_addplot(plot_df['BB_Up'], color='green', width=0.8),
            mpf.make_addplot(plot_df['BB_Mid'], color='orange', width=0.8),
            mpf.make_addplot(plot_df['BB_Low'], color='green', width=0.8),
            mpf.make_addplot(plot_df['RSI'], panel=1, color='yellow', width=1.2),
        ]
        
        h_lines = []
        colors = []
        if signal_info['res_line']: 
            h_lines.append(signal_info['res_line']); colors.append('red')
        if signal_info['support_line']: 
            h_lines.append(signal_info['support_line']); colors.append('green')

        title_text = f"{symbol} [{LTF}] - Trend HTF: {signal_info['htf_trend']}"
        kwargs = dict(hlines=dict(hlines=h_lines, colors=colors, linestyle='-.', linewidths=1)) if h_lines else {}

        mpf.plot(plot_df, type='candle', style=style, addplot=adds, 
                 title=title_text, savefig=dict(fname=filename, bbox_inches='tight'), 
                 volume=False, panel_ratios=(6, 2), **kwargs)
        return filename
    except: return None

# ==========================================
# 5. CORE LOGIC (MTF & STRUCTURE)
# ==========================================
def get_htf_trend(symbol):
    """Mengecek tren di timeframe besar (HTF) menggunakan EMA 50"""
    try:
        bars = exchange.fetch_ohlcv(symbol, HTF, limit=50)
        df_htf = pd.DataFrame(bars, columns=['t','o','h','l','c','v'])
        ema50 = ta.ema(df_htf['c'], length=20).iloc[-1]
        curr_price = df_htf['c'].iloc[-1]
        return "BULLISH 📈" if curr_price > ema50 else "BEARISH 📉"
    except: return "NEUTRAL ⚖️"

def check_market_structure(df, window=5):
    if len(df) < 50: return "Unknown", None, None
    df['is_high'] = df['high'].rolling(window=window*2+1, center=True).max() == df['high']
    df['is_low'] = df['low'].rolling(window=window*2+1, center=True).min() == df['low']
    
    confirmed_data = df.iloc[:-window]
    last_highs = confirmed_data[confirmed_data['is_high']]
    last_lows = confirmed_data[confirmed_data['is_low']]

    if len(last_highs) < 2 or len(last_lows) < 2: return "Sideways", None, None

    h1, h2 = last_highs['high'].iloc[-1], last_highs['high'].iloc[-2]
    l1, l2 = last_lows['low'].iloc[-1], last_lows['low'].iloc[-2]

    if h1 > h2 and l1 > l2: structure = "UPTREND (HH/HL)"
    elif h1 < h2 and l1 < l2: structure = "DOWNTREND (LH/LL)"
    else: structure = "SIDEWAYS"
    
    return structure, h1, l1

# ==========================================
# 6. WORKER PROSES
# ==========================================
def worker_scan(symbol):
    try:
        # 1. Cek Tren HTF Dulu (Filter Utama)
        htf_trend = get_htf_trend(symbol)
        
        # 2. Ambil Data LTF
        bars = exchange.fetch_ohlcv(symbol, LTF, limit=LIMIT)
        df = pd.DataFrame(bars, columns=['timestamp','open','high','low','close','volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')

        # 3. Filter Volume
        current_vol = df['volume'].iloc[-2]
        avg_vol = df['volume'].iloc[-8:-2].mean()
        spike_ratio = current_vol / avg_vol if avg_vol > 0 else 0
        
        if spike_ratio < VOL_MULTIPLIER: return None

        # 4. Indikator & Struktur
        bb = df.ta.bbands(length=20, std=2)
        df['BB_Up'] = bb.iloc[:, 2]; df['BB_Mid'] = bb.iloc[:, 1]; df['BB_Low'] = bb.iloc[:, 0]
        df['BB_Width'] = (df['BB_Up'] - df['BB_Low']) / df['BB_Mid']
        df['BB_Width_Avg'] = df['BB_Width'].rolling(20).mean()
        df['RSI'] = df.ta.rsi(length=14)
        df['MA5_Hi'] = df['high'].rolling(5).mean()
        df['MA5_Lo'] = df['low'].rolling(5).mean()

        structure, h1, l1 = check_market_structure(df)
        c = df.iloc[-2]
        is_squeeze = c['BB_Width'] < c['BB_Width_Avg']
        
        if CARI_BB_DATAR and not is_squeeze: return None

        # 5. Konfirmasi Sinyal (LTF harus Searah HTF)
        res = None
        # SETUP BUY: HTF Bullish & LTF Bullish/Sideways
        if "BULLISH" in htf_trend:
            if c['close'] > df['BB_Mid'].iloc[-2]:
                res = {"tipe": "BUY", "signal": "MTF BULLISH ALIGNMENT", "explanation": "HTF Trend Naik + LTF Volume Spike."}
        
        # SETUP SELL: HTF Bearish & LTF Bearish/Sideways
        elif "BEARISH" in htf_trend:
            if c['close'] < df['BB_Mid'].iloc[-2]:
                res = {"tipe": "SELL", "signal": "MTF BEARISH ALIGNMENT", "explanation": "HTF Trend Turun + LTF Volume Spike."}

        if res:
            res.update({
                'symbol': symbol, 'spike_ratio': spike_ratio, 'df': df, 'time': c['timestamp'],
                'htf_trend': htf_trend, 'structure': structure, 'bb_status': "SQUEEZE" if is_squeeze else "EXPAND",
                'divergence': "None", 'price': c['close'], 'res_line': h1, 'support_line': l1
            })
            return res
    except: pass
    return None

# ==========================================
# 7. MAIN LOOP
# ==========================================
def main():
    print(f"=== VOLUME STRUCTURE MTF BOT ===")
    print(f"HTF: {HTF} | LTF: {LTF} | Vol: {VOL_MULTIPLIER}x")
    
    while True:
        try:
            exchange.load_markets()
            tickers = exchange.fetch_tickers()
            symbols = sorted([t for t in tickers if '/USDT' in t and 'UP/' not in t], 
                             key=lambda x: tickers[x]['quoteVolume'], reverse=True)[:TOP_COIN_COUNT]
            
            alerts = []
            with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_THREADS) as executor:
                results = list(executor.map(worker_scan, symbols))
                alerts = [r for r in results if r]

            for a in alerts:
                if processed_signals.get(a['symbol']) != a['time']:
                    processed_signals[a['symbol']] = a['time']
                    print(f"🚀 MATCH: {a['symbol']} (HTF: {a['htf_trend']})")
                    img = generate_chart(a['df'], a['symbol'], a)
                    if img: send_telegram_alert(a['symbol'], a, img)
            
            print(f"[{datetime.now().strftime('%H:%M')}] Scan Complete. Waiting 5m...")
            time.sleep(300)
        except Exception as e:
            print(f"Error: {e}"); time.sleep(10)

if __name__ == "__main__":
    main()

