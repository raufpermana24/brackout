import asyncio
import ccxt.async_support as ccxt  # Mode Async (Cepat)
import ccxt as ccxt_sync           # Mode Sync (Robust/Fallback)
import pandas as pd
import mplfinance as mpf
import requests
import os
import time
import traceback
from datetime import datetime, timedelta

# ================= KONFIGURASI =================
BINANCE_API_KEY = os.environ.get('BINANCE_API_KEY', 'fZwDMOfBL6rDU9jfUQox64fUAb2RSN48myxMPUGDAINYjmLdqJmUFhVRWLqlsX97')
BINANCE_SECRET_KEY = os.environ.get('BINANCE_API_SECRET', 'FmZNNbIOWIAddxVoLcNowLNW379E6gxyM85Bvy3QzlRMtK1eMApJp6vJtpGHWdWB')
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', '8562793193:AAHDulfzVhhnuPfNfy4Zk6ONBNSNbGwVJ8c')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '-1003819540522')


# Konfigurasi Scanner
TOP_COINS = 400
BATCH_SIZE = 20
DELAY_BATCH = 0.5
TELEGRAM_SEND_DELAY = 5  # Jeda 5 detik sebelum kirim signal

# ================= MEMORY SYSTEM (ANTI-SPAM) =================
SENT_SIGNALS = set()

def is_signal_already_sent(symbol, timeframe, candle_timestamp, signal_type):
    signal_id = f"{symbol}_{timeframe}_{candle_timestamp}_{signal_type}"
    return signal_id in SENT_SIGNALS

def mark_signal_as_sent(symbol, timeframe, candle_timestamp, signal_type):
    signal_id = f"{symbol}_{timeframe}_{candle_timestamp}_{signal_type}"
    SENT_SIGNALS.add(signal_id)

def clean_old_memory():
    if len(SENT_SIGNALS) > 5000:
        SENT_SIGNALS.clear()

# ================= TELEGRAM UTILS =================
def send_telegram_sync(message):
    if not TELEGRAM_TOKEN: return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}, timeout=10)
    except Exception as e:
        print(f"[TG ERROR] {e}")

def send_photo_sync(caption, filepath):
    if not TELEGRAM_TOKEN: return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
        with open(filepath, 'rb') as img:
            requests.post(url, files={'photo': img}, data={'chat_id': TELEGRAM_CHAT_ID, 'caption': caption}, timeout=20)
    except Exception as e:
        print(f"[TG PHOTO ERROR] {e}")

# ================= CHART GENERATOR (ROBUST) =================
def generate_chart(df, symbol, timeframe, extra_info=""):
    filename = f"chart_{symbol.replace('/', '')}_{timeframe}_{int(time.time())}.png"
    try:
        if len(df) < 30: return None

        plot_df = df.tail(60).copy()
        plot_df.set_index('timestamp', inplace=True)

        mc = mpf.make_marketcolors(up='#2ebd85', down='#f6465d', edge='i', wick='i', volume='in', inherit=True)
        s = mpf.make_mpf_style(marketcolors=mc, gridstyle=':', y_on_right=True)

        ap = [
            mpf.make_addplot(plot_df['MA5'], color='cyan', width=1, panel=0),
            mpf.make_addplot(plot_df['MA10'], color='orange', width=1, panel=0),
            mpf.make_addplot(plot_df['MA30'], color='purple', width=2, panel=0),
            mpf.make_addplot(plot_df['RSI'], panel=2, color='#b48eff', ylabel='RSI', width=1.5),
        ]

        mpf.plot(
            plot_df, type='candle', style=s, addplot=ap, volume=True,
            title=f"{symbol} ({timeframe}) {extra_info}",
            panel_ratios=(6,2,2), tight_layout=True,
            hlines=dict(hlines=[70,30], colors=['red','green'], linestyle='-.', linewidths=0.5, alpha=0.5, panel=2),
            savefig=dict(fname=filename, dpi=80, bbox_inches='tight')
        )
        return filename
    except Exception as e:
        return None

# ================= INDICATORS & FETCH LOGIC =================

def calculate_indicators(df):
    df['MA5'] = df['close'].rolling(window=5).mean()
    df['MA10'] = df['close'].rolling(window=10).mean()
    df['MA30'] = df['close'].rolling(window=30).mean()
    
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    df['RSI'] = 100 - (100 / (1 + rs))

    exp12 = df['close'].ewm(span=12, adjust=False).mean()
    exp26 = df['close'].ewm(span=26, adjust=False).mean()
    df['MACD'] = exp12 - exp26
    df['MACD_SIGNAL'] = df['MACD'].ewm(span=9, adjust=False).mean()
    
    df['VolMA'] = df['volume'].rolling(window=20).mean()
    return df

async def calculate_natr(exchange, symbol, timeframe, period):
    try:
        limit_data = period + 20
        bars = await exchange.fetch_ohlcv(symbol, timeframe, limit=limit_data)
        if not bars: return 0.0
        df = pd.DataFrame(bars, columns=['t', 'o', 'high', 'low', 'close', 'v'])
        df['prev_close'] = df['close'].shift(1)
        df['tr'] = df[['high', 'prev_close']].max(axis=1) - df[['low', 'prev_close']].min(axis=1)
        df['atr'] = df['tr'].rolling(window=period).mean()
        last_close = df['close'].iloc[-1]
        return (df['atr'].iloc[-1] / last_close) * 100 if last_close else 0.0
    except:
        return 0.0

async def fetch_data_fast(exchange, symbol, timeframe):
    """Metode 1: Async Cepat"""
    try:
        bars = await exchange.fetch_ohlcv(symbol, timeframe, limit=100)
        if not bars or len(bars) < 35: return None
        df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        return df
    except Exception:
        return None

def fetch_data_robust_fallback(symbol, timeframe):
    """Metode 2: Sync Robust Fallback"""
    print(f"⚠️ [FALLBACK] Mengambil data REST API untuk {symbol}...")
    try:
        fallback_exchange = ccxt_sync.binance({'options': {'defaultType': 'future'}})
        bars = fallback_exchange.fetch_ohlcv(symbol, timeframe, limit=500)
        if not bars: return None
        
        df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        return df
    except Exception as e:
        print(f"❌ [FALLBACK GAGAL] {e}")
        return None

# ================= CORE LOGIC =================

async def get_top_volume_symbols(exchange):
    print(f"🔍 Mengambil Top {TOP_COINS} Koin (Volume Tertinggi)...")
    try:
        markets = await exchange.load_markets()
        tickers = await exchange.fetch_tickers()
        futures = []
        for symbol, data in tickers.items():
            if symbol in markets and markets[symbol].get('quote') == 'USDT' and markets[symbol].get('active'):
                vol = data.get('quoteVolume') or 0
                futures.append({'symbol': symbol, 'volume': vol})
        futures.sort(key=lambda x: x['volume'], reverse=True)
        return [x['symbol'] for x in futures[:TOP_COINS]]
    except Exception as e:
        print(f"Error Symbols: {e}")
        return []

async def analyze_and_alert(exchange, symbol, timeframe):
    # 1. Fetch Fast
    df = await fetch_data_fast(exchange, symbol, timeframe)
    if df is None: return None 

    df = calculate_indicators(df)
    
    last_closed = df.iloc[-2]
    prev_closed = df.iloc[-3]
    candle_ts = last_closed['timestamp'].strftime('%Y-%m-%d %H:%M:%S')

    # --- FILTER VOLUME (MARKET TIDAK NORMAL) ---
    curr_vol = last_closed['volume']
    avg_vol = last_closed['VolMA']
    if avg_vol == 0: return None
    vol_ratio = curr_vol / avg_vol
    
    is_high_vol = vol_ratio > 1.5
    is_low_vol = vol_ratio < 0.5
    
    if not (is_high_vol or is_low_vol): return None # Skip Normal Market

    vol_desc = f"🔥 HIGH ({vol_ratio:.1f}x)" if is_high_vol else f"❄️ LOW ({vol_ratio:.1f}x)"
    signal_type, signal_side = None, None
    has_alert = False

    # --- LOGIKA SIGNAL ---
    if prev_closed['MA5'] <= prev_closed['MA10'] and last_closed['MA5'] > last_closed['MA10']:
        if not is_signal_already_sent(symbol, timeframe, candle_ts, 'GOLDEN_CROSS'):
            signal_type = 'GOLDEN_CROSS'
            signal_side = 'LONG 🟢'
            has_alert = True
    elif timeframe != '5m':
        if last_closed['low'] <= last_closed['MA30'] <= last_closed['high']:
            if not is_signal_already_sent(symbol, timeframe, candle_ts, 'MA30_TOUCH'):
                signal_type = 'MA30_TOUCH'
                signal_side = 'LONG 🟢 (Bounce)' if last_closed['close'] > last_closed['MA30'] else 'SHORT 🔴 (Reject)'
                has_alert = True
    elif vol_ratio > 2.5:
        if not is_signal_already_sent(symbol, timeframe, candle_ts, 'VOL_SPIKE'):
            signal_type = 'VOLUME_SPIKE'
            signal_side = 'INFO ⚠️'
            has_alert = True

    if has_alert:
        # Kunci Sinyal
        mark_signal_as_sent(symbol, timeframe, candle_ts, signal_type)
        
        # JEDA 5 DETIK SEBELUM PROSES LANJUTAN (Sesuai Permintaan)
        # Kita taruh di sini agar tidak memblokir scanning jika tidak ada sinyal
        print(f"   ⏳ Menunggu {TELEGRAM_SEND_DELAY} detik sebelum kirim telegram...")
        await asyncio.sleep(TELEGRAM_SEND_DELAY)

        # Hitung NATR
        natr_task1 = calculate_natr(exchange, symbol, '1m', 30)
        natr_task2 = calculate_natr(exchange, symbol, '5m', 14)
        n1, n5 = await asyncio.gather(natr_task1, natr_task2)
        
        # Buat Chart (Coba Fast)
        chart_file = generate_chart(df, symbol, timeframe, f"| {vol_desc}")
        
        # Fallback Chart (Jika Fast gagal)
        if chart_file is None:
            df_robust = fetch_data_robust_fallback(symbol, timeframe)
            if df_robust is not None:
                df_robust = calculate_indicators(df_robust)
                chart_file = generate_chart(df_robust, symbol, timeframe, f"| {vol_desc} (R)")
        
        rsi_val = last_closed['RSI']
        rsi_stat = "OVERBOUGHT 🔴" if rsi_val > 70 else "OVERSOLD 🟢" if rsi_val < 30 else "Neutral"
        macd_stat = "Bullish 🟢" if last_closed['MACD'] > last_closed['MACD_SIGNAL'] else "Bearish 🔴"
        
        header = "🚀" if "LONG" in signal_side else "🔻"
        if signal_type == 'VOLUME_SPIKE': header = "⚡"
        
        caption = (
            f"{header} **SIGNAL {signal_side}**\n\n"
            f"🪙 `#{symbol.replace('/','')}`\n"
            f"⏱ TF: `{timeframe}` | 💵 ${last_closed['close']}\n\n"
            f"📊 **Volume:** {vol_desc}\n"
            f"📈 **RSI:** {rsi_val:.1f} ({rsi_stat})\n"
            f"📉 **MACD:** {macd_stat}\n\n"
            f"🌪 **NATR:** 1m: `{n1:.2f}%` | 5m: `{n5:.2f}%`\n"
            f"📋 Trigger: {signal_type.replace('_', ' ')}"
        )
        
        print(f"   🔔 SENT: {symbol} {signal_type} ({timeframe})")
        
        if chart_file:
            send_photo_sync(caption, chart_file)
            if os.path.exists(chart_file): os.remove(chart_file)
        else:
            send_telegram_sync(caption)

async def run_scanner_job(active_timeframes):
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 🔄 Memulai Scan: {active_timeframes}")
    
    exchange = ccxt.binance({
        'apiKey': BINANCE_API_KEY, 'secret': BINANCE_SECRET_KEY,
        'enableRateLimit': False, 'options': {'defaultType': 'future'}
    })

    try:
        symbols = await get_top_volume_symbols(exchange)
        if not symbols: return

        for tf in active_timeframes:
            print(f"📊 Scanning {tf} ({len(symbols)} coins)...")
            
            for i in range(0, len(symbols), BATCH_SIZE):
                batch = symbols[i : i + BATCH_SIZE]
                tasks = [analyze_and_alert(exchange, sym, tf) for sym in batch]
                await asyncio.gather(*tasks)
                await asyncio.sleep(DELAY_BATCH)
            
    finally:
        await exchange.close()
        clean_old_memory()
    print(f"✅ Selesai Scan {active_timeframes}")

async def main_scheduler():
    print("=== BOT FINAL HYBRID (5s Delay + TF 1h,4h,6h) ===")
    
    while True:
        now = datetime.now()
        # Hitung waktu ke 5 menit berikutnya
        minutes_to_next = 5 - (now.minute % 5)
        next_run = now + timedelta(minutes=minutes_to_next)
        next_run = next_run.replace(second=0, microsecond=0)
        scheduled_time = next_run + timedelta(seconds=15)
        
        seconds_to_wait = (scheduled_time - datetime.now()).total_seconds()
        if seconds_to_wait < 0: seconds_to_wait += 300
            
        print(f"\n⏳ Menunggu {int(seconds_to_wait)} detik...")
        await asyncio.sleep(seconds_to_wait)
        
        # --- PENENTUAN TIMEFRAME ---
        run_minute = next_run.minute
        run_hour = next_run.hour
        
        active_tfs = ['5m'] # 5m selalu discan
        
        if run_minute % 15 == 0: active_tfs.append('15m')
        if run_minute % 30 == 0: active_tfs.append('30m')
        
        # Setiap jam (Menit 00)
        if run_minute == 0:
            active_tfs.append('1h')
            
            # Setiap 4 Jam (00, 04, 08, 12, 16, 20)
            if run_hour % 4 == 0:
                active_tfs.append('4h')
                
            # Setiap 6 Jam (00, 06, 12, 18)
            if run_hour % 6 == 0:
                active_tfs.append('6h')
            
        await run_scanner_job(active_tfs)

if __name__ == "__main__":
    try:
        asyncio.run(main_scheduler())
    except KeyboardInterrupt:
        print("\nBot Stopped")


Detail Perubahan Penting:
Jeda 5 Detik (TELEGRAM_SEND_DELAY):
Di dalam fungsi analyze_and_alert, setelah sinyal terdeteksi valid (if has_alert:), saya menambahkan baris:
print(f"   ⏳ Menunggu {TELEGRAM_SEND_DELAY} detik sebelum kirim telegram...")
await asyncio.sleep(TELEGRAM_SEND_DELAY)




