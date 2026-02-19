import asyncio
import json
import websockets
import aiohttp
import pandas as pd
import mplfinance as mpf
import os
import time
import sys
import traceback
import matplotlib
matplotlib.use('Agg') # Mode Headless (Wajib untuk Server/VPS)
from datetime import datetime

# ================= KONFIGURASI =================
BINANCE_API_KEY = os.environ.get('BINANCE_API_KEY', 'fZwDMOfBL6rDU9jfUQox64fUAb2RSN48myxMPUGDAINYjmLdqJmUFhVRWLqlsX97')
BINANCE_SECRET_KEY = os.environ.get('BINANCE_API_SECRET', 'FmZNNbIOWIAddxVoLcNowLNW379E6gxyM85Bvy3QzlRMtK1eMApJp6vJtpGHWdWB')
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', '8562793193:AAHDulfzVhhnuPfNfy4Zk6ONBNSNbGwVJ8c')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '-1003819540522')

# Konfigurasi Scanner
TIMEFRAMES = ['15m', '1h', '4h'] # Timeframe target
LIMIT_HISTORY = 120              # Jumlah candle per koin per timeframe
SEND_DELAY = 5                   # Jeda pengiriman sinyal

# Endpoint Binance Futures
WS_URL = "wss://fstream.binance.com/stream?streams="
REST_URL = "https://fapi.binance.com"

# ================= MEMORY STORE =================
# Struktur: DATA_STORE['BTCUSDT']['15m'] = DataFrame
DATA_STORE = {}
# Set untuk mencatat sinyal agar tidak duplikat
SENT_SIGNALS = set()

# ================= UTILS =================
def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

# --- ASYNC TELEGRAM ---
async def send_telegram_async(session, message):
    if not TELEGRAM_TOKEN: return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
        async with session.post(url, data=payload) as resp:
            await resp.text()
    except Exception as e:
        log(f"[TG ERROR] {e}")

async def send_photo_async(session, caption, file_path):
    if not TELEGRAM_TOKEN: return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    try:
        with open(file_path, 'rb') as f:
            form = aiohttp.FormData()
            form.add_field('chat_id', TELEGRAM_CHAT_ID)
            form.add_field('caption', caption)
            form.add_field('photo', f, filename='chart.png')
            async with session.post(url, data=form) as resp:
                await resp.text()
    except Exception as e:
        log(f"[TG FOTO ERROR] {e}")

# ================= CHARTING ENGINE =================
def generate_chart_task(df, symbol, timeframe, extra_info):
    filename = f"chart_{symbol}_{timeframe}_{int(time.time())}.png"
    try:
        if df is None or len(df) < 50: return None

        plot_df = df.tail(80).copy()
        if 'timestamp' in plot_df.columns:
            plot_df.set_index('timestamp', inplace=True)
        if not isinstance(plot_df.index, pd.DatetimeIndex):
            plot_df.index = pd.to_datetime(plot_df.index)

        mc = mpf.make_marketcolors(up='#2ebd85', down='#f6465d', edge='i', wick='i', volume='in', inherit=True)
        s = mpf.make_mpf_style(marketcolors=mc, gridstyle=':', y_on_right=True)

        add_plots = [
            mpf.make_addplot(plot_df['MA5'], color='cyan', width=0.8, panel=0),
            mpf.make_addplot(plot_df['MA10'], color='orange', width=0.8, panel=0),
            mpf.make_addplot(plot_df['MA30'], color='magenta', width=1.5, panel=0),
            mpf.make_addplot(plot_df['BB_Upper'], color='gray', width=0.5, panel=0),
            mpf.make_addplot(plot_df['BB_Lower'], color='gray', width=0.5, panel=0),
            mpf.make_addplot(plot_df['RSI'], panel=2, color='#b48eff', ylabel='RSI', width=1.2, ylim=(0, 100)),
            mpf.make_addplot([70]*len(plot_df), panel=2, color='#ff5252', width=0.6, linestyle='--'),
            mpf.make_addplot([30]*len(plot_df), panel=2, color='#69f0ae', width=0.6, linestyle='--'),
            mpf.make_addplot(plot_df['OBV'], panel=3, color='dodgerblue', ylabel='OBV', width=1.2),
        ]

        # Panel Open Interest (Jika ada)
        panel_ratios = (6, 1, 1, 1)
        if 'OpenInterest' in plot_df.columns and not plot_df['OpenInterest'].isnull().all():
            add_plots.append(mpf.make_addplot(plot_df['OpenInterest'], panel=4, color='gold', ylabel='OI', width=1.2))
            panel_ratios = (5, 1, 1, 1, 1)

        fill_bb = dict(y1=plot_df['BB_Upper'].values, y2=plot_df['BB_Lower'].values, color='gray', alpha=0.1)

        mpf.plot(
            plot_df, type='candle', style=s, addplot=add_plots, volume=True,
            title=f"\n{symbol} ({timeframe}) {extra_info}",
            panel_ratios=panel_ratios, tight_layout=True, datetime_format='%H:%M',
            fill_between=[fill_bb, dict(y1=30, y2=70, color='#2c2c2c', alpha=0.1, panel=2)],
            savefig=dict(fname=filename, dpi=100, bbox_inches='tight')
        )
        return filename
    except Exception:
        return None

# ================= DATA FETCHING (REST) =================
async def fetch_open_interest(session, symbol, timeframe):
    try:
        url = f"{REST_URL}/fapi/v1/openInterestHist"
        params = {'symbol': symbol.upper(), 'period': timeframe, 'limit': LIMIT_HISTORY}
        async with session.get(url, params=params) as resp:
            data = await resp.json()
            if isinstance(data, list) and len(data) > 0:
                oi_df = pd.DataFrame(data)
                oi_df['timestamp'] = pd.to_datetime(oi_df['timestamp'], unit='ms')
                oi_df['sumOpenInterest'] = oi_df['sumOpenInterest'].astype(float)
                oi_df.set_index('timestamp', inplace=True)
                return oi_df['sumOpenInterest']
    except: pass
    return None

async def fetch_data_fallback(session, symbol, timeframe):
    try:
        url = f"{REST_URL}/fapi/v1/klines?symbol={symbol.upper()}&interval={timeframe}&limit={LIMIT_HISTORY}"
        async with session.get(url) as resp:
            data = await resp.json()
            if isinstance(data, list) and len(data) > 0:
                df = pd.DataFrame(data, columns=['t', 'o', 'h', 'l', 'c', 'v', 'T', 'q', 'n', 'V', 'Q', 'B'])
                df = df[['t', 'o', 'h', 'l', 'c', 'v']].astype(float)
                df.columns = ['timestamp', 'open', 'high', 'low', 'close', 'volume']
                df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
                return calculate_indicators(df)
    except: return None
    return None

# ================= INDIKATOR =================
def calculate_indicators(df):
    try:
        df['MA5'] = df['close'].rolling(window=5).mean()
        df['MA10'] = df['close'].rolling(window=10).mean()
        df['MA30'] = df['close'].rolling(window=30).mean()
        
        # BB
        df['BB_Middle'] = df['close'].rolling(window=20).mean()
        std = df['close'].rolling(window=20).std()
        df['BB_Upper'] = df['BB_Middle'] + (2 * std)
        df['BB_Lower'] = df['BB_Middle'] - (2 * std)
        df['BB_Width'] = (df['BB_Upper'] - df['BB_Lower']) / df['BB_Middle']

        # RSI & MACD
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        df['RSI'] = 100 - (100 / (1 + rs))

        exp12 = df['close'].ewm(span=12, adjust=False).mean()
        exp26 = df['close'].ewm(span=26, adjust=False).mean()
        df['MACD'] = exp12 - exp26
        df['MACD_SIGNAL'] = df['MACD'].ewm(span=9, adjust=False).mean()
        
        # Vol & OBV
        df['VolMA'] = df['volume'].rolling(window=20).mean()
        df['tr'] = df[['high', 'close']].max(axis=1) - df[['low', 'close']].min(axis=1) 
        df['atr'] = df['tr'].rolling(window=14).mean()
        
        direction = df['close'].diff().apply(lambda x: 1 if x > 0 else (-1 if x < 0 else 0))
        df['OBV'] = (direction * df['volume']).cumsum()
        
        df.fillna(method='bfill', inplace=True)
        return df
    except: return df

# ================= LOGIKA ANALISA & ALERT =================

async def handle_alert_async(symbol, timeframe, signal_type, signal_side, df, vol_desc, is_startup=False):
    log(f"🔔 ALERT: {symbol} {signal_type} ({timeframe})")
    
    loop = asyncio.get_running_loop()
    
    # Ambil OI (Async)
    async with aiohttp.ClientSession() as session:
        oi_series = await fetch_open_interest(session, symbol, timeframe)
        
        chart_df = df.copy()
        if oi_series is not None:
            if 'timestamp' in chart_df.columns: chart_df.set_index('timestamp', inplace=True)
            chart_df['OpenInterest'] = oi_series
            chart_df['OpenInterest'].fillna(method='ffill', inplace=True)
            chart_df.reset_index(inplace=True)
        else:
            chart_df['OpenInterest'] = None

        chart_file = await loop.run_in_executor(None, generate_chart_task, chart_df, symbol, timeframe, f"| {vol_desc}")
        
        if chart_file is None:
             df_fallback = await fetch_data_fallback(session, symbol, timeframe)
             if df_fallback is not None:
                 chart_file = await loop.run_in_executor(None, generate_chart_task, df_fallback, symbol, timeframe, f"| {vol_desc} (R)")

        last = df.iloc[-1]
        caption = (
            f"{'🚀' if 'LONG' in signal_side else '🔻'} {'**STARTUP**' if is_startup else '**LIVE**'} **{signal_side}**\n\n"
            f"🪙 `#{symbol}`\n"
            f"⏱ TF: `{timeframe}` | 💵 ${last['close']}\n\n"
            f"📊 **Vol:** {vol_desc}\n"
            f"📉 **BB:** {'Squeeze' if last['BB_Width'] < 0.05 else 'Wide'}\n"
            f"📈 **RSI:** {last['RSI']:.1f}\n"
            f"🌪 **NATR:** `{(last['atr']/last['close']*100):.2f}%`\n"
            f"💧 **OBV:** {last['OBV']:.0f}\n\n"
            f"📋 Trigger: {signal_type.replace('_', ' ')}"
        )

        if chart_file and os.path.exists(chart_file):
            await send_photo_async(session, caption, chart_file)
            try: os.remove(chart_file)
            except: pass
        else:
            await send_telegram_async(session, caption)

async def analyze_logic(symbol, timeframe, df, is_startup=False):
    last = df.iloc[-1]
    prev = df.iloc[-2]
    candle_ts = last['timestamp'].strftime('%Y-%m-%d %H:%M:%S')

    avg_vol = last['VolMA']
    if avg_vol == 0: return
    vol_ratio = last['volume'] / avg_vol
    
    is_high_vol = vol_ratio > 1.5
    is_low_vol = vol_ratio < 0.5
    
    # Filter Market Normal (Skip)
    if not (is_high_vol or is_low_vol): return 

    vol_desc = f"🔥 HIGH ({vol_ratio:.1f}x)" if is_high_vol else f"❄️ LOW"
    signal_type, signal_side, has_alert = None, None, False

    if prev['MA5'] <= prev['MA10'] and last['MA5'] > last['MA10']:
        signal_type, signal_side, has_alert = 'GOLDEN_CROSS', 'LONG 🟢', True
    elif (last['low'] <= last['MA30'] <= last['high']):
        side = 'LONG 🟢 (Bounce)' if last['close'] > last['MA30'] else 'SHORT 🔴 (Reject)'
        signal_type, signal_side, has_alert = 'MA30_TOUCH', side, True
    elif vol_ratio > 3.0:
        signal_type, signal_side, has_alert = 'EXTREME_VOL', 'INFO ⚠️', True

    if has_alert:
        sig_id = f"{symbol}_{timeframe}_{candle_ts}_{signal_type}"
        if sig_id not in SENT_SIGNALS:
            SENT_SIGNALS.add(sig_id)
            if len(SENT_SIGNALS) > 10000: SENT_SIGNALS.clear()
            asyncio.create_task(handle_alert_async(symbol, timeframe, signal_type, signal_side, df, vol_desc, is_startup))

# ================= GET ALL SYMBOLS & INIT =================

async def get_all_active_symbols():
    """Mengambil SEMUA koin USDT-M yang statusnya TRADING"""
    log("🔍 Mengambil data SEMUA Koin Futures (Exchange Info)...")
    async with aiohttp.ClientSession() as session:
        async with session.get(f'{REST_URL}/fapi/v1/exchangeInfo') as resp:
            data = await resp.json()
            
    symbols = []
    for s in data['symbols']:
        if s['quoteAsset'] == 'USDT' and s['status'] == 'TRADING':
            symbols.append(s['symbol'])
            
    log(f"✅ Ditemukan {len(symbols)} Koin Aktif untuk dipantau.")
    return symbols

async def download_all_history(symbols):
    """
    MASSIVE DOWNLOAD: Mengambil history untuk SEMUA koin di 3 timeframe.
    Menggunakan Semaphore agar tidak kena ban IP.
    """
    log("🚀 MEMULAI DOWNLOAD BESAR (Ini akan memakan waktu 1-2 menit)...")
    async with aiohttp.ClientSession() as session:
        # Batasi hanya 20 request paralel agar aman dari rate limit
        sem = asyncio.Semaphore(20) 

        async def fetch_and_store(sym, tf):
            async with sem:
                try:
                    url = f"{REST_URL}/fapi/v1/klines?symbol={sym}&interval={tf}&limit={LIMIT_HISTORY}"
                    async with session.get(url) as resp:
                        # Handle Rate Limit Header jika perlu (biasanya 2400/min)
                        if resp.status == 429:
                            log(f"⚠️ Rate Limit Hit! Sleeping 5s...")
                            await asyncio.sleep(5)
                            return

                        data = await resp.json()
                        if isinstance(data, list) and len(data) > 0:
                            df = pd.DataFrame(data, columns=['t', 'o', 'h', 'l', 'c', 'v', 'T', 'q', 'n', 'V', 'Q', 'B'])
                            df = df[['t', 'o', 'h', 'l', 'c', 'v']].astype(float)
                            df.columns = ['timestamp', 'open', 'high', 'low', 'close', 'volume']
                            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
                            
                            df = calculate_indicators(df)
                            
                            if sym not in DATA_STORE: DATA_STORE[sym] = {}
                            DATA_STORE[sym][tf] = df
                except Exception as e:
                    # log(f"Gagal download {sym} {tf}: {e}")
                    pass

        # Buat ribuan task (Jml Koin x 3 Timeframe)
        tasks = [fetch_and_store(s, t) for s in symbols for t in TIMEFRAMES]
        
        # Eksekusi dengan progress bar sederhana
        total = len(tasks)
        for i, f in enumerate(asyncio.as_completed(tasks)):
            if i % 100 == 0:
                print(f"📥 Progress: {i}/{total} files downloaded...", end='\r')
        
    print(f"\n✅ DOWNLOAD SELESAI. Semua data tersimpan di RAM.")

async def scan_entire_memory():
    """
    Melakukan scan terhadap semua data yang ada di RAM (DATA_STORE)
    """
    log("🧠 Memulai Analisa Memori (Scan Semesta)...")
    count = 0
    for symbol, tfs in DATA_STORE.items():
        for tf, df in tfs.items():
            # Analisa Logic (Startup Mode)
            await analyze_logic(symbol, tf, df, is_startup=True)
            count += 1
            # Jeda mikro agar CPU tidak spike 100%
            if count % 100 == 0: await asyncio.sleep(0.01)
    
    log("✅ Analisa Memori Selesai. Menunggu update WebSocket...")

# ================= WEBSOCKET HANDLER =================

async def process_stream_data(symbol, timeframe, kline):
    if not kline['x']: return 
    try:
        # Jika koin baru listing/tidak ada di memory, abaikan dulu
        if symbol not in DATA_STORE or timeframe not in DATA_STORE[symbol]: return

        df = DATA_STORE[symbol][timeframe]
        ts = pd.to_datetime(kline['t'], unit='ms')
        
        new_row = {
            'timestamp': ts, 'open': float(kline['o']), 'high': float(kline['h']),
            'low': float(kline['l']), 'close': float(kline['c']), 'volume': float(kline['v'])
        }

        if df.iloc[-1]['timestamp'] == ts:
            for col, val in new_row.items(): df.at[df.index[-1], col] = val
        else:
            new_df = pd.DataFrame([new_row])
            df = pd.concat([df, new_df], ignore_index=True)
            if len(df) > LIMIT_HISTORY + 10: df = df.iloc[10:].reset_index(drop=True)
        
        df = calculate_indicators(df)
        DATA_STORE[symbol][timeframe] = df

        await analyze_logic(symbol, timeframe, df, is_startup=False)
    except: pass

async def listen_socket(streams):
    url = WS_URL + "/".join(streams)
    while True:
        try:
            log(f"🔌 WS Connected ({len(streams)} streams)")
            async with websockets.connect(url, ping_interval=None) as ws:
                while True:
                    msg = await ws.recv()
                    data = json.loads(msg)
                    if 'data' in data:
                        p = data['data']
                        await process_stream_data(p['s'], p['k']['i'], p['k'])
        except Exception:
            await asyncio.sleep(5)

async def main():
    log("=== BOT FUTURES UNIVERSE SCAN (ALL COINS) ===")
    
    # 1. Ambil SEMUA Simbol
    symbols = await get_all_active_symbols()
    if not symbols: return

    # 2. Download SEMUA Data ke RAM
    await download_all_history(symbols)
    
    # 3. Analisa SEMUA Data di RAM (Startup Scan)
    await scan_entire_memory()
    
    # 4. Subscribe WebSocket untuk Semua Koin
    log("🌐 Menyiapkan WebSocket Stream untuk seluruh pasar...")
    all_streams = [f"{sym.lower()}@kline_{tf}" for sym in symbols for tf in TIMEFRAMES]
    
    # Pecah koneksi (Max 50-100 stream per koneksi agar stabil)
    BATCH = 50
    tasks = []
    for i in range(0, len(all_streams), BATCH):
        tasks.append(listen_socket(all_streams[i:i+BATCH]))
        
    await asyncio.gather(*tasks)

if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Stopped")


