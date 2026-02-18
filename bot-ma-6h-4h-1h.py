import asyncio
import json
import websockets
import aiohttp
import pandas as pd
import mplfinance as mpf
import requests
import os
import time
import sys
import traceback
from datetime import datetime

# ================= KONFIGURASI =================
BINANCE_API_KEY = os.environ.get('BINANCE_API_KEY', 'fZwDMOfBL6rDU9jfUQox64fUAb2RSN48myxMPUGDAINYjmLdqJmUFhVRWLqlsX97')
BINANCE_SECRET_KEY = os.environ.get('BINANCE_API_SECRET', 'FmZNNbIOWIAddxVoLcNowLNW379E6gxyM85Bvy3QzlRMtK1eMApJp6vJtpGHWdWB')
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', '8562793193:AAHDulfzVhhnuPfNfy4Zk6ONBNSNbGwVJ8c')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '-1003819540522')

# Setting Scanner
TOP_COINS = 50          # Pantau 50 Koin Teratas (Agar sangat cepat & ringan)
TIMEFRAMES = ['1h', '4h', '6h'] # FOKUS TIMEFRAME BESAR
LIMIT_HISTORY = 120     # Buffer history di RAM
SEND_DELAY = 5          # Jeda 5 detik sebelum kirim telegram

# Endpoint Binance Futures
WS_URL = "wss://fstream.binance.com/stream?streams="
REST_URL = "https://fapi.binance.com"

# ================= MEMORY STORE =================
DATA_STORE = {}
SENT_SIGNALS = set()

# ================= UTILS =================
def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

def send_telegram_sync(message):
    if not TELEGRAM_TOKEN: return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}, timeout=5)
    except Exception as e:
        log(f"[TG ERROR] {e}")

def send_photo_sync(caption, filepath):
    if not TELEGRAM_TOKEN: return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
        with open(filepath, 'rb') as img:
            requests.post(url, files={'photo': img}, data={'chat_id': TELEGRAM_CHAT_ID, 'caption': caption}, timeout=20)
    except Exception as e:
        log(f"[TG FOTO ERROR] {e}")

# ================= CHARTING & INDICATORS =================

def calculate_indicators(df):
    try:
        # MA
        df['MA5'] = df['close'].rolling(window=5).mean()
        df['MA10'] = df['close'].rolling(window=10).mean()
        df['MA30'] = df['close'].rolling(window=30).mean()
        
        # RSI
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        df['RSI'] = 100 - (100 / (1 + rs))

        # MACD
        exp12 = df['close'].ewm(span=12, adjust=False).mean()
        exp26 = df['close'].ewm(span=26, adjust=False).mean()
        df['MACD'] = exp12 - exp26
        df['MACD_SIGNAL'] = df['MACD'].ewm(span=9, adjust=False).mean()
        
        # Vol MA & ATR
        df['VolMA'] = df['volume'].rolling(window=20).mean()
        df['prev_close'] = df['close'].shift(1)
        df['tr'] = df[['high', 'prev_close']].max(axis=1) - df[['low', 'prev_close']].min(axis=1)
        df['atr'] = df['tr'].rolling(window=14).mean()
        
        return df
    except Exception:
        return df

def generate_chart(df, symbol, timeframe, extra_info=""):
    """
    Membuat Chart: Panel 1 (Candle+MA), Panel 2 (Vol), Panel 3 (RSI)
    """
    filename = f"chart_{symbol}_{timeframe}_{int(time.time())}.png"
    try:
        if len(df) < 35: return None 
        
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
            title=f"FUTURES: {symbol} ({timeframe}) {extra_info}",
            panel_ratios=(6,2,2), tight_layout=True,
            hlines=dict(hlines=[70,30], colors=['red','green'], linestyle='-.', linewidths=0.5, alpha=0.5, panel=2),
            savefig=dict(fname=filename, dpi=80, bbox_inches='tight')
        )
        return filename
    except Exception:
        return None

# ================= REST API FALLBACK (METODE 2) =================

async def fetch_data_fallback(symbol, timeframe):
    """
    Mengambil data bersih via REST API jika WebSocket data bermasalah saat mau screenshot.
    """
    log(f"⚠️ [FALLBACK] Fetching REST API data for {symbol} ({timeframe})...")
    async with aiohttp.ClientSession() as session:
        try:
            url = f"{REST_URL}/fapi/v1/klines?symbol={symbol.upper()}&interval={timeframe}&limit={LIMIT_HISTORY}"
            async with session.get(url) as resp:
                data = await resp.json()
                if isinstance(data, list) and len(data) > 0:
                    df = pd.DataFrame(data, columns=['t', 'o', 'h', 'l', 'c', 'v', 'T', 'q', 'n', 'V', 'Q', 'B'])
                    df = df[['t', 'o', 'h', 'l', 'c', 'v']].astype(float)
                    df.columns = ['timestamp', 'open', 'high', 'low', 'close', 'volume']
                    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
                    
                    df = calculate_indicators(df)
                    return df
        except Exception as e:
            log(f"❌ [FALLBACK GAGAL] {e}")
    return None

# ================= WEBSOCKET LOGIC (METODE 1) =================

async def get_top_futures_symbols():
    log("🔍 Mengambil Top Koin Futures (Volume Tertinggi)...")
    async with aiohttp.ClientSession() as session:
        async with session.get(f'{REST_URL}/fapi/v1/ticker/24hr') as resp:
            data = await resp.json()
            
    futures = []
    for item in data:
        if item['symbol'].endswith('USDT'):
            futures.append({'symbol': item['symbol'], 'volume': float(item['quoteVolume'])})
            
    futures.sort(key=lambda x: x['volume'], reverse=True)
    top_list = [x['symbol'] for x in futures[:TOP_COINS]]
    return top_list

async def initialize_data(symbols):
    """Pre-load data via REST agar indikator siap hitung sejak detik pertama"""
    log("⏳ Pre-loading history data (1h, 4h, 6h)...")
    async with aiohttp.ClientSession() as session:
        tasks = []
        async def fetch(sym, tf):
            try:
                url = f"{REST_URL}/fapi/v1/klines?symbol={sym}&interval={tf}&limit={LIMIT_HISTORY}"
                async with session.get(url) as resp:
                    data = await resp.json()
                    if isinstance(data, list) and len(data) > 0:
                        df = pd.DataFrame(data, columns=['t', 'o', 'h', 'l', 'c', 'v', 'T', 'q', 'n', 'V', 'Q', 'B'])
                        df = df[['t', 'o', 'h', 'l', 'c', 'v']].astype(float)
                        df.columns = ['timestamp', 'open', 'high', 'low', 'close', 'volume']
                        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
                        if sym not in DATA_STORE: DATA_STORE[sym] = {}
                        DATA_STORE[sym][tf] = df
            except: pass

        all_combinations = [(sym, tf) for sym in symbols for tf in TIMEFRAMES]
        # Batching request agar tidak kena limit IP
        for i in range(0, len(all_combinations), 30):
            chunk = all_combinations[i:i + 30]
            await asyncio.gather(*[fetch(s, t) for s, t in chunk])
            await asyncio.sleep(0.1)
    log("✅ History Loaded. WebSocket Ready.")

async def handle_signal_alert(symbol, timeframe, signal_type, signal_side, df, vol_desc):
    """
    Fungsi pengirim Alert dengan mekanisme FALLBACK CHART
    """
    loop = asyncio.get_running_loop()
    
    # 1. Jeda 5 Detik (Agar indikator final dan tidak spam)
    log(f"🔔 ALERT: {symbol} {signal_type} ({timeframe}). Waiting {SEND_DELAY}s...")
    await asyncio.sleep(SEND_DELAY)
    
    last = df.iloc[-1]
    
    # 2. Coba Generate Chart dengan Data WebSocket (Cepat)
    chart_file = await loop.run_in_executor(None, generate_chart, df, symbol, timeframe, f"| {vol_desc}")
    
    # 3. FALLBACK: Jika chart gagal, ambil data ulang via REST API
    if chart_file is None:
        log(f"⚠️ Chart WebSocket gagal. Mencoba REST API Fallback...")
        df_fallback = await fetch_data_fallback(symbol, timeframe)
        
        if df_fallback is not None:
            chart_file = await loop.run_in_executor(None, generate_chart, df_fallback, symbol, timeframe, f"| {vol_desc} (REST)")
    
    # Siapkan Pesan Telegram
    price = last['close']
    rsi = last['RSI']
    rsi_stat = "OVERBOUGHT" if rsi > 70 else "OVERSOLD" if rsi < 30 else "Neutral"
    macd_stat = "Bullish" if last['MACD'] > last['MACD_SIGNAL'] else "Bearish"
    
    # Hitung NATR
    natr = (last['atr'] / price * 100) if price else 0

    header = "🚀" if "LONG" in signal_side else "🔻"
    if signal_type == 'EXTREME_VOL': header = "⚡"

    caption = (
        f"{header} **FUTURES SIGNAL {signal_side}**\n\n"
        f"🪙 `#{symbol}`\n"
        f"⏱ TF: `{timeframe}` | 💵 ${price}\n\n"
        f"📊 **Volume:** {vol_desc}\n"
        f"📈 **RSI:** {rsi:.1f} ({rsi_stat})\n"
        f"📉 **MACD:** {macd_stat}\n\n"
        f"🌪 **NATR:** `{natr:.2f}%`\n"
        f"📋 Trigger: {signal_type.replace('_', ' ')}"
    )

    # Kirim ke Telegram
    if chart_file:
        await loop.run_in_executor(None, send_photo_sync, caption, chart_file)
        if os.path.exists(chart_file): os.remove(chart_file)
    else:
        # Jika fallback pun gagal gambar, kirim teks saja
        await loop.run_in_executor(None, send_telegram_sync, caption)

async def analyze_logic(symbol, timeframe, df):
    last = df.iloc[-1]
    prev = df.iloc[-2]
    candle_ts = last['timestamp'].strftime('%Y-%m-%d %H:%M:%S')

    # Filter Volume (Abnormal Only)
    avg_vol = last['VolMA']
    if avg_vol == 0: return
    vol_ratio = last['volume'] / avg_vol
    
    is_high_vol = vol_ratio > 1.5
    is_low_vol = vol_ratio < 0.5
    
    # SKIP MARKET NORMAL (Request User)
    if not (is_high_vol or is_low_vol): return 

    vol_desc = f"🔥 HIGH ({vol_ratio:.1f}x)" if is_high_vol else f"❄️ LOW ({vol_ratio:.1f}x)"
    signal_type, signal_side = None, None
    has_alert = False

    # Logic
    if prev['MA5'] <= prev['MA10'] and last['MA5'] > last['MA10']:
        signal_type = 'GOLDEN_CROSS'
        signal_side = 'LONG 🟢'
        has_alert = True
    elif (last['low'] <= last['MA30'] <= last['high']):
        signal_type = 'MA30_TOUCH'
        signal_side = 'LONG 🟢 (Bounce)' if last['close'] > last['MA30'] else 'SHORT 🔴 (Reject)'
        has_alert = True
    elif vol_ratio > 3.0:
        signal_type = 'EXTREME_VOL'
        signal_side = 'INFO ⚠️'
        has_alert = True

    if has_alert:
        # Anti Spam Check
        sig_id = f"{symbol}_{timeframe}_{candle_ts}_{signal_type}"
        if sig_id in SENT_SIGNALS: return
        SENT_SIGNALS.add(sig_id)
        if len(SENT_SIGNALS) > 5000: SENT_SIGNALS.clear()
        
        # Panggil Handler Sinyal
        await handle_signal_alert(symbol, timeframe, signal_type, signal_side, df, vol_desc)

async def process_stream_data(symbol, timeframe, kline):
    try:
        is_closed = kline['x']
        if symbol not in DATA_STORE or timeframe not in DATA_STORE[symbol]: return

        df = DATA_STORE[symbol][timeframe]
        ts = pd.to_datetime(kline['t'], unit='ms')
        
        new_row = {
            'timestamp': ts, 'open': float(kline['o']), 'high': float(kline['h']),
            'low': float(kline['l']), 'close': float(kline['c']), 'volume': float(kline['v'])
        }

        if df.iloc[-1]['timestamp'] == ts:
            df.iloc[-1] = list(new_row.values())
        else:
            new_df = pd.DataFrame([new_row])
            df = pd.concat([df, new_df], ignore_index=True)
            if len(df) > LIMIT_HISTORY + 10: df = df.iloc[10:].reset_index(drop=True)
        
        DATA_STORE[symbol][timeframe] = df

        if is_closed:
            df = calculate_indicators(df)
            await analyze_logic(symbol, timeframe, df)

    except Exception:
        pass

async def listen_socket(streams):
    url = WS_URL + "/".join(streams)
    while True:
        try:
            log(f"🔌 Connecting Batch ({len(streams)})...")
            async with websockets.connect(url) as ws:
                log("✅ WS Connected.")
                while True:
                    msg = await ws.recv()
                    data = json.loads(msg)
                    if 'data' in data:
                        p = data['data']
                        await process_stream_data(p['s'], p['k']['i'], p['k'])
        except Exception:
            await asyncio.sleep(5)

async def main():
    log("=== BOT FUTURES FAST SCAN (1h, 4h, 6h) ===")
    
    symbols = await get_top_futures_symbols()
    if not symbols: return

    await initialize_data(symbols)
    
    all_streams = []
    for sym in symbols:
        for tf in TIMEFRAMES:
            all_streams.append(f"{sym.lower()}@kline_{tf}")
            
    # Batch connection (Max 40 stream per koneksi agar stabil)
    BATCH_SIZE = 40
    tasks = []
    for i in range(0, len(all_streams), BATCH_SIZE):
        tasks.append(listen_socket(all_streams[i:i+BATCH_SIZE]))
        
    await asyncio.gather(*tasks)

if __name__ == "__main__":
    try:
        if sys.platform == 'win32':
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBot Stopped")


