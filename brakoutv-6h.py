import sys
import os
import time
from datetime import datetime
import concurrent.futures  # Untuk Multithreading (Kecepatan Tinggi)

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
    sys.exit(f"Library Error: {e}. Install dulu: pip install ccxt pandas pandas_ta mplfinance requests")

# ==========================================
# 2. KONFIGURASI PENGGUNA
# ==========================================
API_KEY = os.environ.get('BINANCE_API_KEY', 'fZwDMOfBL6rDU9jfUQox64fUAb2RSN48myxMPUGDAINYjmLdqJmUFhVRWLqlsX97')
API_SECRET = os.environ.get('BINANCE_API_SECRET', 'FmZNNbIOWIAddxVoLcNowLNW379E6gxyM85Bvy3QzlRMtK1eMApJp6vJtpGHWdWB')

# Telegram Config (Isi manual jika tidak pakai env var)
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', '8000712659:AAHltp77nGuakOzW9QMgQpVqnd5f1KgEsKA') 
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '-1003896189739')

# Setting Bot
TIMEFRAME = '6h'       # Timeframe Eksekusi (DIUBAH KE 6 JAM)
LIMIT = 200             # Data history
TOP_COIN_COUNT = 300    # TARGET: 300 KOIN
MAX_THREADS = 10        # 10 Worker agar scan 300 koin lebih cepat

OUTPUT_FOLDER = 'smart_bbma_results'
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

# Memori agar tidak spam sinyal yang sama
# Format: {'BTC/USDT': '2023-10-27 10:00:00'}
processed_candles = {} 

# ==========================================
# 3. KONEKSI EXCHANGE
# ==========================================
exchange = ccxt.binance({
    'apiKey': API_KEY, 'secret': API_SECRET,
    'options': {'defaultType': 'future'},
    'enableRateLimit': True, # WAJIB TRUE agar ccxt otomatis menahan rate limit
})

# ==========================================
# 4. FUNGSI TELEGRAM
# ==========================================
def send_telegram_alert(symbol, signal_data, image_path):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    
    caption = (
        f"🧠 <b>SMART BBMA PRO (Top 300)</b>\n"
        f"💎 <b>Coin:</b> {symbol}\n"
        f"⏱ <b>Timeframe:</b> {TIMEFRAME}\n"
        f"--------------------------------\n"
        f"📢 <b>SIGNAL: {signal_data['tipe']}</b>\n"
        f"🎯 <b>Setup:</b> {signal_data['signal']}\n"
        f"💰 <b>Harga:</b> {signal_data['price']}\n"
        f"--------------------------------\n"
        f"📝 <b>PENJELASAN ANALISA:</b>\n"
        f"{signal_data['explanation']}\n"
        f"--------------------------------\n"
        f"<i>Valid Close Candle ✅</i>"
    )

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    try:
        with open(image_path, "rb") as img:
            requests.post(url, data={'chat_id': TELEGRAM_CHAT_ID, 'caption': caption, 'parse_mode': 'HTML'}, files={'photo': img}, timeout=20)
    except Exception as e:
        print(f"Gagal kirim TG: {e}")

# ==========================================
# 5. DATA FETCHING & INDICATORS
# ==========================================
def get_top_symbols(limit=300):
    try:
        # print(f"Mengambil daftar {limit} koin teratas...")
        exchange.load_markets()
        tickers = exchange.fetch_tickers()
        # Filter hanya USDT
        valid_tickers = [t for t in tickers.values() if '/USDT' in t['symbol'] and 'USDC' not in t['symbol'] and 'UP/' not in t['symbol'] and 'DOWN/' not in t['symbol']]
        # Sortir berdasarkan Volume Transaksi (Quote Volume)
        sorted_tickers = sorted(valid_tickers, key=lambda x: x['quoteVolume'] if x['quoteVolume'] else 0, reverse=True)
        return [t['symbol'] for t in sorted_tickers[:limit]]
    except Exception as e: 
        print(f"Gagal ambil simbol: {e}")
        return []

def fetch_ohlcv(symbol):
    try:
        # Mengambil data candle
        bars = exchange.fetch_ohlcv(symbol, TIMEFRAME, limit=LIMIT)
        df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        return df
    except: return None

def add_indicators(df):
    # --- INDIKATOR BBMA ORIGINAL ---
    bb = df.ta.bbands(length=20, std=2)
    if bb is not None:
        df['BB_Up'] = bb.iloc[:, 2]; df['BB_Mid'] = bb.iloc[:, 1]; df['BB_Low'] = bb.iloc[:, 0]
    
    # Moving Averages (MA5 & MA10 High/Low)
    df['MA5_Hi'] = df['high'].rolling(5).mean()
    df['MA5_Lo'] = df['low'].rolling(5).mean()
    df['MA10_Hi'] = df['high'].rolling(10).mean()
    df['MA10_Lo'] = df['low'].rolling(10).mean()
    
    # EMA 50 untuk Trend
    df['EMA_50'] = df.ta.ema(length=50)

    # --- TAMBAHAN: INDIKATOR AI BREAKOUT & VOLUME ---
    # 1. Donchian Channel (Support/Resistance Dinamis 20 periode)
    df['DC_High'] = df['high'].rolling(20).max().shift(1) # Shift 1 agar tidak repaint
    df['DC_Low'] = df['low'].rolling(20).min().shift(1)
    
    # 2. Volume Moving Average
    df['Vol_MA'] = df['volume'].rolling(20).mean()
    
    # 3. Average Candle Range
    df['Body_Size'] = abs(df['close'] - df['open'])
    df['Avg_Range'] = (df['high'] - df['low']).rolling(20).mean()

    return df

def generate_chart(df, symbol, signal_info):
    try:
        filename = f"{OUTPUT_FOLDER}/{symbol.replace('/','-')}_{signal_info['tipe']}.png"
        plot_df = df.tail(60).set_index('timestamp')
        
        style = mpf.make_mpf_style(base_mpf_style='nightclouds', rc={'font.size': 8})
        adds = [
            mpf.make_addplot(plot_df['BB_Up'], color='green', width=0.8),
            mpf.make_addplot(plot_df['BB_Mid'], color='orange', width=0.8),
            mpf.make_addplot(plot_df['BB_Low'], color='green', width=0.8),
            mpf.make_addplot(plot_df['MA5_Hi'], color='cyan', width=0.6),
            mpf.make_addplot(plot_df['MA5_Lo'], color='magenta', width=0.6),
            mpf.make_addplot(plot_df['MA10_Hi'], color='blue', width=0.5), # Ditambahkan MA10
            mpf.make_addplot(plot_df['MA10_Lo'], color='red', width=0.5),  # Ditambahkan MA10
        ]
        if 'EMA_50' in plot_df.columns:
            adds.append(mpf.make_addplot(plot_df['EMA_50'], color='yellow', width=1.5))

        if "BREAKOUT" in signal_info['signal'] or "WHALE" in signal_info['signal']:
             adds.append(mpf.make_addplot(plot_df['DC_High'], color='white', linestyle='dotted', width=0.5))
             adds.append(mpf.make_addplot(plot_df['DC_Low'], color='white', linestyle='dotted', width=0.5))

        mpf.plot(plot_df, type='candle', style=style, addplot=adds, 
                 title=f"{symbol} - {signal_info['signal']}", 
                 savefig=dict(fname=filename, bbox_inches='tight'), volume=True)
        return filename
    except: return None

# ==========================================
# 6. LOGIKA SMART ANALYSIS (LENGKAP)
# ==========================================
def analyze_market_structure(df):
    if df.empty or 'BB_Up' not in df.columns: return None
    c = df.iloc[-2] # Close Candle
    
    signal_data = None
    
    # Trend Filter
    trend_status = "SIDEWAYS"
    if c['close'] > c.get('EMA_50', 0): trend_status = "BULLISH"
    elif c['close'] < c.get('EMA_50', 9999999): trend_status = "BEARISH"

    # ===============================================
    # SETUP 1: EXTREME (MA5 Keluar BB)
    # ===============================================
    if c['MA5_Hi'] > c['BB_Up']:
        signal_data = {
            "signal": "EXTREME", "tipe": "SELL",
            "explanation": "MA5 High keluar Top BB. Sinyal awal pembalikan arah (Reversal). Siap-siap koreksi."
        }
    elif c['MA5_Lo'] < c['BB_Low']:
        signal_data = {
            "signal": "EXTREME", "tipe": "BUY",
            "explanation": "MA5 Low keluar Low BB. Sinyal awal pembalikan arah (Reversal). Siap-siap pantulan."
        }

    # ===============================================
    # SETUP 2: MHV (Market Hilang Volume)
    # Harga gagal tembus BB setelah Extreme/Momentum
    # ===============================================
    elif signal_data is None:
        # MHV BUY: Low sempat tembus/dekat Low BB, tapi Close di dalam (Rejection)
        if c['low'] <= c['BB_Low'] and c['close'] > c['BB_Low'] and c['close'] > c['open']:
             signal_data = {
                "signal": "MHV (Market Hilang Volume)", "tipe": "BUY",
                "explanation": "Harga gagal menembus Low BB (Rejection). Potensi pembalikan arah naik karena kehilangan momentum jual."
            }
        # MHV SELL: High sempat tembus/dekat Top BB, tapi Close di dalam (Rejection)
        elif c['high'] >= c['BB_Up'] and c['close'] < c['BB_Up'] and c['close'] < c['open']:
             signal_data = {
                "signal": "MHV (Market Hilang Volume)", "tipe": "SELL",
                "explanation": "Harga gagal menembus Top BB (Rejection). Potensi pembalikan arah turun karena kehilangan momentum beli."
            }

    # ===============================================
    # KAIDAH: CSA (Candle Arah)
    # Perubahan arah harga (Close melewati MA5 & MA10)
    # ===============================================
    if signal_data is None:
        # CSA BUY: Close di atas MA5 Hi & MA10 Hi
        if c['close'] > c['MA5_Hi'] and c['close'] > c['MA10_Hi']:
             signal_data = {
                "signal": "CSA (Candle Arah)", "tipe": "BUY",
                "explanation": "Candle Close di atas MA5 & MA10 High. Konfirmasi awal perubahan trend naik."
            }
        # CSA SELL: Close di bawah MA5 Lo & MA10 Lo
        elif c['close'] < c['MA5_Lo'] and c['close'] < c['MA10_Lo']:
             signal_data = {
                "signal": "CSA (Candle Arah)", "tipe": "SELL",
                "explanation": "Candle Close di bawah MA5 & MA10 Low. Konfirmasi awal perubahan trend turun."
            }

    # ===============================================
    # KAIDAH: CSM (Candle Momentum)
    # Kekuatan Trend (Close di luar BB)
    # ===============================================
    if signal_data is None: # Prioritas CSM di bawah Extreme/MHV tapi penting
        if c['close'] > c['BB_Up']:
            signal_data = {
                "signal": "CSM (MOMENTUM)", "tipe": "BUY",
                "explanation": "Candle Close Tebal di luar Top BB. Indikasi kekuatan trend BULLISH yang sangat kuat."
            }
        elif c['close'] < c['BB_Low']:
            signal_data = {
                "signal": "CSM (MOMENTUM)", "tipe": "SELL",
                "explanation": "Candle Close Tebal di luar Low BB. Indikasi kekuatan trend BEARISH yang sangat kuat."
            }

    # ===============================================
    # SETUP 3: RE-ENTRY (Setup Teraman)
    # Koreksi setelah CSM/CSA ke area MA5/MA10
    # ===============================================
    if signal_data is None:
        if trend_status == "BULLISH" and c['close'] > c['BB_Mid']:
            # Harga menyentuh area MA5/MA10 Low
            if c['low'] <= c['MA5_Lo'] or c['low'] <= c['MA10_Lo']: 
                signal_data = {
                    "signal": "RE-ENTRY", "tipe": "BUY",
                    "explanation": "Trend BULLISH. Harga koreksi menyentuh area MA5/MA10 Low. Titik Entry Low Risk."
                }
        
        elif trend_status == "BEARISH" and c['close'] < c['BB_Mid']:
            # Harga menyentuh area MA5/MA10 High
            if c['high'] >= c['MA5_Hi'] or c['high'] >= c['MA10_Hi']: 
                signal_data = {
                    "signal": "RE-ENTRY", "tipe": "SELL",
                    "explanation": "Trend BEARISH. Harga koreksi menyentuh area MA5/MA10 High. Titik Entry Low Risk."
                }

    # ===============================================
    # STRATEGI AI & WHALE (Backup jika BBMA tidak ada sinyal)
    # ===============================================
    if signal_data is None:
        is_vol_spike = c['volume'] > (c['Vol_MA'] * 1.5)
        
        # AI Breakout
        if c['close'] > c['DC_High'] and is_vol_spike:
            signal_data = {"signal": "AI BREAKOUT", "tipe": "BUY", "explanation": "Breakout Resistance 20-Hari + Volume."}
        elif c['close'] < c['DC_Low'] and is_vol_spike:
            signal_data = {"signal": "AI BREAKOUT", "tipe": "SELL", "explanation": "Breakout Support 20-Hari + Volume."}
            
        # Whale Detector
        elif signal_data is None:
            vol_ratio = c['volume'] / (c['Vol_MA'] + 1)
            is_significant_candle = c['Body_Size'] > (0.5 * c['Avg_Range'])

            if vol_ratio > 2.5 and is_significant_candle:
                if c['close'] > c['open']:
                    signal_data = {"signal": "WHALE PUMP 🐳", "tipe": "BUY", "explanation": f"Volume Spike {vol_ratio:.1f}x."}
                else:
                    signal_data = {"signal": "WHALE DUMP 🐋", "tipe": "SELL", "explanation": f"Volume Spike {vol_ratio:.1f}x."}

    if signal_data:
        signal_data['price'] = c['close']
        signal_data['time'] = c['timestamp']
        return signal_data
    
    return None

# ==========================================
# 7. WORKER THREAD
# ==========================================
def worker_scan(symbol):
    try:
        # Fetch Data
        df = fetch_ohlcv(symbol)
        if df is None or len(df) < 55: return None
        
        # Analisa
        df = add_indicators(df)
        result = analyze_market_structure(df)
        
        if result:
            result['symbol'] = symbol
            result['df'] = df
            return result
    except:
        pass
    return None

# ==========================================
# 8. MAIN ENGINE
# ==========================================
def main():
    print(f"=== SMART BBMA PRO COMPLETE (EXTREME, MHV, RE-ENTRY, CSA, CSM) ===")
    print(f"Mode: {MAX_THREADS} Threads | TF: {TIMEFRAME}")
    print(f"Target: Top {TOP_COIN_COUNT} Liquid Coins")
    
    global processed_candles

    # Hitung detik timeframe untuk sync (6 Jam = 21600 detik)
    tf_seconds = 6 * 3600 if '6h' in TIMEFRAME else 3600 

    while True:
        try:
            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Mengambil daftar koin...")
            symbols = get_top_symbols(TOP_COIN_COUNT)
            
            if not symbols:
                print("Gagal mengambil simbol. Ulangi...")
                time.sleep(5)
                continue

            print(f"🚀 Memulai Scan {len(symbols)} Koin (Cek Candle Close)...")
            
            alerts_queue = []
            start_t = time.time()
            
            # --- PARALLEL EXECUTION ---
            with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_THREADS) as executor:
                futures = {executor.submit(worker_scan, sym): sym for sym in symbols}
                
                completed = 0
                for future in concurrent.futures.as_completed(futures):
                    res = future.result()
                    if res:
                        alerts_queue.append(res)
                    completed += 1
                    sys.stdout.write(f"\rProgress: {completed}/{len(symbols)} | Sinyal: {len(alerts_queue)}")
                    sys.stdout.flush()
            
            duration = time.time() - start_t
            print(f"\n✅ Selesai dalam {duration:.2f} detik. Sinyal: {len(alerts_queue)}")

            # --- KIRIM HASIL ---
            for alert in alerts_queue:
                sym = alert['symbol']
                candle_time = alert['time']

                if processed_candles.get(sym) != candle_time:
                    processed_candles[sym] = candle_time
                    
                    print(f"🔥 KIRIM TELEGRAM: {sym} [{alert['tipe']} - {alert['signal']}]")
                    img = generate_chart(alert['df'], sym, alert)
                    if img: send_telegram_alert(sym, alert, img)
            
            # --- SYNC WAKTU ---
            now_ts = time.time()
            next_run = (int(now_ts) // tf_seconds + 1) * tf_seconds
            sleep_time = next_run - now_ts + 10 
            
            next_time_str = datetime.fromtimestamp(now_ts + sleep_time).strftime('%H:%M:%S')
            print(f"⏳ Scan Selesai. Menunggu penutupan candle berikutnya pada {next_time_str}...")
            print(f"💤 Tidur selama {int(sleep_time)} detik...")
            
            time.sleep(sleep_time)

        except KeyboardInterrupt:
            print("\nBerhenti.")
            break
        except Exception as e:
            print(f"Error Main Loop: {e}")
            time.sleep(10)

if __name__ == "__main__":
    main()
