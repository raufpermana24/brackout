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
from datetime import datetime
from io import BytesIO

# ================= KONFIGURASI =================
BINANCE_API_KEY = os.environ.get('BINANCE_API_KEY', 'fZwDMOfBL6rDU9jfUQox64fUAb2RSN48myxMPUGDAINYjmLdqJmUFhVRWLqlsX97')
BINANCE_SECRET_KEY = os.environ.get('BINANCE_API_SECRET', 'FmZNNbIOWIAddxVoLcNowLNW379E6gxyM85Bvy3QzlRMtK1eMApJp6vJtpGHWdWB')
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', '8562793193:AAHDulfzVhhnuPfNfy4Zk6ONBNSNbGwVJ8c')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '-1003819540522')


# Konfigurasi Scanner
TOP_COINS = 40          # Pantau 40 Koin Teratas
TIMEFRAMES = ['1h', '4h', '6h'] # Timeframe Besar
LIMIT_HISTORY = 120     # Buffer history

# Endpoint
WS_URL = "wss://fstream.binance.com/stream?streams="
REST_URL = "https://fapi.binance.com"

# ================= MEMORY STORE =================
DATA_STORE = {}
SENT_SIGNALS = set()

# ================= UTILS =================
def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

# --- ASYNC TELEGRAM (KUNCI KECEPATAN) ---
async def send_telegram_async(session, message):
    if not TELEGRAM_TOKEN: return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
        async with session.post(url, data=payload) as resp:
            await resp.text() # Pastikan request selesai
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

# ================= CHARTING ENGINE (CPU BOUND) =================
def generate_chart_task(df, symbol, timeframe, extra_info):
    """
    Fungsi ini akan dijalankan di Thread Pool agar tidak memblokir WebSocket
    """
    filename = f"chart_{symbol}_{timeframe}_{int(time.time())}.png"
    try:
        if df is None or len(df) < 50: return None

        # Copy data agar tidak konflik dengan update websocket
        plot_df = df.tail(80).copy()
        if 'timestamp' in plot_df.columns:
            plot_df.set_index('timestamp', inplace=True)
        
        mc = mpf.make_marketcolors(up='#2ebd85', down='#f6465d', edge='i', wick='i', volume='in', inherit=True)
        s = mpf.make_mpf_style(marketcolors=mc, gridstyle=':', y_on_right=True)

        add_plots = [
            mpf.make_addplot(plot_df['MA5'], color='cyan', width=1.0, panel=0),
            mpf.make_addplot(plot_df['MA10'], color='orange', width=1.0, panel=0),
            mpf.make_addplot(plot_df['MA30'], color='magenta', width=2.0, panel=0),
            mpf.make_addplot(plot_df['BB_Upper'], color='gray', width=0.5, panel=0),
            mpf.make_addplot(plot_df['BB_Lower'], color='gray', width=0.5, panel=0),
            mpf.make_addplot(plot_df['RSI'], panel=2, color='#b48eff', ylabel='RSI', width=1.5, ylim=(0, 100))
        ]

        fill_bb = dict(y1=plot_df['BB_Upper'].values, y2=plot_df['BB_Lower'].values, color='gray', alpha=0.1)

        mpf.plot(
            plot_df, type='candle', style=s, addplot=add_plots, volume=True,
            title=f"\n{symbol} ({timeframe}) {extra_info}",
            panel_ratios=(6, 2, 2), tight_layout=True, datetime_format='%H:%M',
            fill_between=[fill_bb, dict(y1=30, y2=70, color='#2c2c2c', alpha=0.1, panel=2)],
            hlines=dict(hlines=[70, 30], colors=['#ff5252', '#69f0ae'], linestyle='--', linewidths=1.0, panel=2),
            savefig=dict(fname=filename, dpi=80, bbox_inches='tight')
        )
        return filename
    except Exception:
        return None

# ================= INDIKATOR =================
def calculate_indicators(df):
    try:
        # Optimasi: Hitung indikator hanya jika data berubah
        df['MA5'] = df['close'].rolling(window=5).mean()
        df['MA10'] = df['close'].rolling(window=10).mean()
        df['MA30'] = df['close'].rolling(window=30).mean()
        
        # BB
        df['BB_Middle'] = df['close'].rolling(window=20).mean()
        std = df['close'].rolling(window=20).std()
        df['BB_Upper'] = df['BB_Middle'] + (2 * std)
        df['BB_Lower'] = df['BB_Middle'] - (2 * std)
        df['BB_Width'] = (df['BB_Upper'] - df['BB_Lower']) / df['BB_Middle']

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
        df['tr'] = df[['high', 'close']].max(axis=1) - df[['low', 'close']].min(axis=1) # Simplified TR
        df['atr'] = df['tr'].rolling(window=14).mean()
        
        df.fillna(method='bfill', inplace=True)
        return df
    except Exception:
        return df

# ================= REST FALLBACK =================
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
    except:
        return None
    return None

# ================= LOGIKA UTAMA =================

async def handle_alert_async(symbol, timeframe, signal_type, signal_side, df, vol_desc):
    """
    Menangani Alert secara ASYNC agar tidak memblokir loop utama.
    """
    log(f"🔔 DETECTED: {symbol} {signal_type} ({timeframe}) - Processing...")
    
    loop = asyncio.get_running_loop()
    
    # 1. Generate Chart di Thread Lain (Non-Blocking)
    chart_file = await loop.run_in_executor(None, generate_chart_task, df, symbol, timeframe, f"| {vol_desc}")
    
    # 2. Persiapkan Data
    last = df.iloc[-1]
    price = last['close']
    rsi_stat = "OVERBOUGHT" if last['RSI'] > 70 else "OVERSOLD" if last['RSI'] < 30 else "Neutral"
    macd_stat = "Bullish" if last['MACD'] > last['MACD_SIGNAL'] else "Bearish"
    natr = (last['atr'] / price * 100) if price else 0
    bb_stat = "Squeeze" if last['BB_Width'] < 0.05 else "Wide"

    header = "🚀" if "LONG" in signal_side else "🔻"
    if signal_type == 'EXTREME_VOL': header = "⚡"

    caption = (
        f"{header} **FUTURES SIGNAL {signal_side}**\n\n"
        f"🪙 `#{symbol}`\n"
        f"⏱ TF: `{timeframe}` | 💵 ${price}\n\n"
        f"📊 **Volume:** {vol_desc}\n"
        f"📉 **BB:** {bb_stat}\n"
        f"📈 **RSI:** {last['RSI']:.1f} ({rsi_stat})\n"
        f"📉 **MACD:** {macd_stat}\n"
        f"🌪 **NATR:** `{natr:.2f}%`\n\n"
        f"📋 Trigger: {signal_type.replace('_', ' ')}"
    )

    # 3. Kirim via Aiohttp (Non-Blocking)
    async with aiohttp.ClientSession() as session:
        if chart_file:
            # Fallback check: jika chart dari WS gagal, coba ambil REST
            if not os.path.exists(chart_file):
                df_fallback = await fetch_data_fallback(session, symbol, timeframe)
                if df_fallback is not None:
                    chart_file = await loop.run_in_executor(None, generate_chart_task, df_fallback, symbol, timeframe, f"| {vol_desc} (R)")

            if chart_file and os.path.exists(chart_file):
                await send_photo_async(session, caption, chart_file)
                try: os.remove(chart_file)
                except: pass
            else:
                await send_telegram_async(session, caption)
        else:
            # Chart failed total, send text
            await send_telegram_async(session, caption)
            
    log(f"✅ SENT: {symbol} {signal_type}")

async def process_stream_data(symbol, timeframe, kline):
    # Optimasi: Hanya proses jika candle close
    if not kline['x']: return 

    try:
        if symbol not in DATA_STORE or timeframe not in DATA_STORE[symbol]: return

        df = DATA_STORE[symbol][timeframe]
        ts = pd.to_datetime(kline['t'], unit='ms')
        
        # Update Row Terakhir
        new_row = {
            'timestamp': ts, 'open': float(kline['o']), 'high': float(kline['h']),
            'low': float(kline['l']), 'close': float(kline['c']), 'volume': float(kline['v'])
        }

        # Update DataFrame dengan cara efisien
        if df.iloc[-1]['timestamp'] == ts:
            # Jika timestamp sama, update nilai saja (lebih cepat dari concat)
            for col, val in new_row.items():
                df.at[df.index[-1], col] = val
        else:
            # Candle baru
            new_df = pd.DataFrame([new_row])
            df = pd.concat([df, new_df], ignore_index=True)
            if len(df) > LIMIT_HISTORY + 10: df = df.iloc[10:].reset_index(drop=True)
        
        # Hitung Indikator
        df = calculate_indicators(df)
        DATA_STORE[symbol][timeframe] = df

        # --- LOGIKA SIGNAL ---
        last = df.iloc[-1]
        prev = df.iloc[-2]
        
        # Filter Volume
        if last['VolMA'] == 0: return
        vol_ratio = last['volume'] / last['VolMA']
        if not (vol_ratio > 1.5 or vol_ratio < 0.5): return # Skip Normal

        vol_desc = f"🔥 HIGH ({vol_ratio:.1f}x)" if vol_ratio > 1.5 else "❄️ LOW"
        
        has_alert = False
        sig_type = ""
        sig_side = ""

        # Cek Signal
        if prev['MA5'] <= prev['MA10'] and last['MA5'] > last['MA10']:
            sig_type, sig_side, has_alert = 'GOLDEN_CROSS', 'LONG 🟢', True
        elif (last['low'] <= last['MA30'] <= last['high']):
            side = 'LONG 🟢 (Bounce)' if last['close'] > last['MA30'] else 'SHORT 🔴 (Reject)'
            sig_type, sig_side, has_alert = 'MA30_TOUCH', side, True
        elif vol_ratio > 3.0:
            sig_type, sig_side, has_alert = 'EXTREME_VOL', 'INFO ⚠️', True

        if has_alert:
            candle_ts = last['timestamp'].strftime('%Y-%m-%d %H:%M:%S')
            sig_id = f"{symbol}_{timeframe}_{candle_ts}_{sig_type}"
            
            if sig_id not in SENT_SIGNALS:
                SENT_SIGNALS.add(sig_id)
                if len(SENT_SIGNALS) > 5000: SENT_SIGNALS.clear()
                
                # FIRE AND FORGET: Buat task baru untuk handle alert agar websocket tidak nunggu
                asyncio.create_task(handle_alert_async(symbol, timeframe, sig_type, sig_side, df, vol_desc))

    except Exception:
        # traceback.print_exc()
        pass

async def listen_socket(streams):
    url = WS_URL + "/".join(streams)
    while True:
        try:
            log(f"🔌 Connecting {len(streams)} streams...")
            async with websockets.connect(url, ping_interval=None) as ws:
                log("✅ Connected.")
                while True:
                    msg = await ws.recv()
                    data = json.loads(msg)
                    if 'data' in data:
                        p = data['data']
                        # process_stream_data sekarang non-blocking karena logic berat di create_task
                        await process_stream_data(p['s'], p['k']['i'], p['k'])
        except Exception as e:
            log(f"⚠️ WS Error: {e}. Retry 5s...")
            await asyncio.sleep(5)

async def initialize_data(symbols):
    log("⏳ Fast Initialization (REST API)...")
    async with aiohttp.ClientSession() as session:
        # Semaphore untuk limit concurrency agar tidak kena ban IP
        sem = asyncio.Semaphore(10) 

        async def fetch(sym, tf):
            async with sem:
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

        tasks = [fetch(s, t) for s in symbols for t in TIMEFRAMES]
        await asyncio.gather(*tasks)
    log("✅ Data Initialized.")

async def get_top_symbols():
    async with aiohttp.ClientSession() as session:
        async with session.get(f'{REST_URL}/fapi/v1/ticker/24hr') as resp:
            data = await resp.json()
    
    futures = []
    for item in data:
        if item['symbol'].endswith('USDT'):
            futures.append({'symbol': item['symbol'], 'volume': float(item['quoteVolume'])})
    futures.sort(key=lambda x: x['volume'], reverse=True)
    return [x['symbol'] for x in futures[:TOP_COINS]]

async def main():
    log("=== BOT SUPER FAST (Async IO) ===")
    symbols = await get_top_symbols()
    if not symbols: return

    await initialize_data(symbols)
    
    all_streams = [f"{sym.lower()}@kline_{tf}" for sym in symbols for tf in TIMEFRAMES]
    
    # Split connections
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


