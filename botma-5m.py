import asyncio
import ccxt.async_support as ccxt
import pandas as pd
import mplfinance as mpf
import requests
import os
import time
from datetime import datetime, timedelta

# ================= KONFIGURASI =================
BINANCE_API_KEY = os.environ.get('BINANCE_API_KEY', 'fZwDMOfBL6rDU9jfUQox64fUAb2RSN48myxMPUGDAINYjmLdqJmUFhVRWLqlsX97')
BINANCE_SECRET_KEY = os.environ.get('BINANCE_API_SECRET', 'FmZNNbIOWIAddxVoLcNowLNW379E6gxyM85Bvy3QzlRMtK1eMApJp6vJtpGHWdWB')
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', '8562793193:AAHDulfzVhhnuPfNfy4Zk6ONBNSNbGwVJ8c')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '-1003822778016')


# Konfigurasi Scanner
LIMIT_CANDLES = 100
TOP_COINS = 400        # SCAN 400 KOIN TERATAS
BATCH_SIZE = 20       
DELAY_BATCH = 0.5     

# ================= MEMORY SYSTEM =================
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

# ================= UTILITIES =================

def send_telegram_sync(message):
    if not TELEGRAM_TOKEN: return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}, timeout=5)
    except Exception as e:
        print(f"[ERROR] TG: {e}")

def send_photo_sync(caption, filepath):
    if not TELEGRAM_TOKEN: return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
        with open(filepath, 'rb') as img:
            requests.post(url, files={'photo': img}, data={'chat_id': TELEGRAM_CHAT_ID, 'caption': caption}, timeout=10)
    except Exception as e:
        print(f"[ERROR] Foto: {e}")

def generate_chart(df, symbol, timeframe, extra_info=""):
    filename = f"chart_{symbol.replace('/', '')}_{timeframe}_{int(time.time())}.png"
    try:
        plot_df = df.tail(60).copy()
        plot_df.set_index('timestamp', inplace=True)
        mc = mpf.make_marketcolors(up='#2ebd85', down='#f6465d', inherit=True)
        s = mpf.make_mpf_style(marketcolors=mc, gridstyle=':', y_on_right=True)
        
        ap = [
            mpf.make_addplot(plot_df['MA5'], color='cyan', width=1),
            mpf.make_addplot(plot_df['MA10'], color='orange', width=1),
            mpf.make_addplot(plot_df['MA30'], color='purple', width=2),
        ]
        
        title_text = f"{symbol} ({timeframe}) {extra_info}"
        mpf.plot(plot_df, type='candle', style=s, addplot=ap, title=title_text,
                 savefig=dict(fname=filename, dpi=80, bbox_inches='tight'), volume=False)
        return filename
    except:
        return None

# ================= INDICATORS =================

async def calculate_natr(exchange, symbol, timeframe, period):
    try:
        limit_data = period + 20
        bars = await exchange.fetch_ohlcv(symbol, timeframe, limit=limit_data)
        if not bars or len(bars) < period + 1: return 0.0
        df = pd.DataFrame(bars, columns=['t', 'o', 'high', 'low', 'close', 'v'])
        df['prev_close'] = df['close'].shift(1)
        df['tr'] = df[['high', 'prev_close']].max(axis=1) - df[['low', 'prev_close']].min(axis=1)
        df['atr'] = df['tr'].rolling(window=period).mean()
        last_close = df['close'].iloc[-1]
        if last_close == 0: return 0.0
        return (df['atr'].iloc[-1] / last_close) * 100
    except:
        return 0.0

def calculate_rsi(df, period=14):
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def calculate_macd(df, fast=12, slow=26, signal=9):
    exp12 = df['close'].ewm(span=fast, adjust=False).mean()
    exp26 = df['close'].ewm(span=slow, adjust=False).mean()
    macd = exp12 - exp26
    signal_line = macd.ewm(span=signal, adjust=False).mean()
    return macd, signal_line

# ================= CORE LOGIC =================

async def get_top_volume_symbols(exchange):
    """Mengambil Top 400 Koin Futures berdasarkan Volume"""
    print(f"🔍 Mengambil Top {TOP_COINS} Koin berdasarkan Volume...")
    try:
        markets = await exchange.load_markets()
        tickers = await exchange.fetch_tickers()
        futures = []
        for symbol, data in tickers.items():
            if symbol in markets and markets[symbol].get('quote') == 'USDT' and markets[symbol].get('active'):
                futures.append({'symbol': symbol, 'volume': data.get('quoteVolume', 0)})
        
        # Sort Volume Tertinggi
        futures.sort(key=lambda x: x['volume'], reverse=True)
        
        # Ambil Top N
        top_symbols = [x['symbol'] for x in futures[:TOP_COINS]]
        print(f"✅ Siap Scan {len(top_symbols)} Koin.")
        return top_symbols
    except Exception as e:
        print(f"Error Fetch Symbols: {e}")
        return []

async def process_coin(exchange, symbol, timeframe):
    try:
        bars = await exchange.fetch_ohlcv(symbol, timeframe, limit=LIMIT_CANDLES)
        if not bars or len(bars) < 35: return None

        df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')

        # --- INDIKATOR UTAMA ---
        df['MA5'] = df['close'].rolling(window=5).mean()
        df['MA10'] = df['close'].rolling(window=10).mean()
        df['MA30'] = df['close'].rolling(window=30).mean()
        
        # --- INDIKATOR TAMBAHAN (RSI, MACD, VOL) ---
        df['RSI'] = calculate_rsi(df)
        df['MACD'], df['MACD_SIGNAL'] = calculate_macd(df)
        df['VolMA'] = df['volume'].rolling(window=20).mean() # Rata-rata Volume 20 candle

        last_closed = df.iloc[-2]
        prev_closed = df.iloc[-3]
        candle_ts = last_closed['timestamp'].strftime('%Y-%m-%d %H:%M:%S')
        
        # --- FILTER PENTING: CEK VOLUME ABNORMAL ---
        curr_vol = last_closed['volume']
        avg_vol = last_closed['VolMA']
        vol_ratio = curr_vol / avg_vol if avg_vol > 0 else 0
        
        is_high_vol = vol_ratio > 1.5   # Volume Tinggi (> 1.5x rata-rata)
        is_low_vol = vol_ratio < 0.5    # Volume Rendah (< 0.5x rata-rata)
        
        # JIKA VOLUME NORMAL (0.5 s/d 1.5), SKIP KOIN INI
        if not (is_high_vol or is_low_vol):
            return None  # Stop, jangan proses lebih lanjut

        # Jika lolos filter (market tidak normal), lanjut analisa...
        
        vol_desc = f"🔥 HIGH ({vol_ratio:.1f}x)" if is_high_vol else f"❄️ LOW ({vol_ratio:.1f}x)"
        
        result = {
            'symbol': symbol, 'type': None, 'side': None,
            'data': last_closed, 'df': df, 'tf': timeframe, 'ts': candle_ts,
            'natr_1m': 0.0, 'natr_5m': 0.0,
            'rsi': 0, 'macd_status': '', 'vol_status': vol_desc
        }
        has_alert = False

        # --- ANALISA TEKNIKAL ---
        
        # 1. RSI Status
        rsi_val = last_closed['RSI']
        rsi_desc = "Neutral"
        if rsi_val > 70: rsi_desc = "OVERBOUGHT (>70) 🔴"
        elif rsi_val < 30: rsi_desc = "OVERSOLD (<30) 🟢"
        elif rsi_val > 60: rsi_desc = "Strong Bull"
        elif rsi_val < 40: rsi_desc = "Strong Bear"
        
        # 2. MACD Status
        macd_val = last_closed['MACD']
        sig_val = last_closed['MACD_SIGNAL']
        macd_desc = "Bullish 🟢" if macd_val > sig_val else "Bearish 🔴"

        result['rsi'] = f"{rsi_val:.1f} ({rsi_desc})"
        result['macd_status'] = macd_desc

        # --- LOGIKA TRIGGER SIGNAL ---
        
        # Trigger 1: GOLDEN CROSS
        if prev_closed['MA5'] <= prev_closed['MA10'] and last_closed['MA5'] > last_closed['MA10']:
            if not is_signal_already_sent(symbol, timeframe, candle_ts, 'GOLDEN_CROSS'):
                result['type'] = 'GOLDEN_CROSS'
                result['side'] = 'LONG 🟢'
                has_alert = True
                mark_signal_as_sent(symbol, timeframe, candle_ts, 'GOLDEN_CROSS')

        # Trigger 2: MA30 TOUCH (Skip 5m)
        elif timeframe != '5m': 
            if last_closed['low'] <= last_closed['MA30'] <= last_closed['high']:
                if not is_signal_already_sent(symbol, timeframe, candle_ts, 'MA30_TOUCH'):
                    result['type'] = 'MA30_TOUCH'
                    if last_closed['close'] > last_closed['MA30']:
                        result['side'] = 'LONG 🟢 (Bounce)'
                    else:
                        result['side'] = 'SHORT 🔴 (Reject)'
                    has_alert = True
                    mark_signal_as_sent(symbol, timeframe, candle_ts, 'MA30_TOUCH')
        
        # Trigger 3: EXTREME VOLUME ONLY (Jika tidak ada cross/touch, tapi volume sangat ekstrem)
        # Misal volume naik > 2.5x rata-rata, kita anggap ini signal tersendiri
        elif vol_ratio > 2.5:
             if not is_signal_already_sent(symbol, timeframe, candle_ts, 'VOL_SPIKE'):
                result['type'] = 'VOLUME_SPIKE'
                result['side'] = 'INFO ⚠️'
                has_alert = True
                mark_signal_as_sent(symbol, timeframe, candle_ts, 'VOL_SPIKE')

        if has_alert:
            # Hitung NATR jika ada sinyal valid & volume tidak normal
            task1 = calculate_natr(exchange, symbol, '1m', 30)
            task2 = calculate_natr(exchange, symbol, '5m', 14)
            natr_results = await asyncio.gather(task1, task2)
            result['natr_1m'] = natr_results[0]
            result['natr_5m'] = natr_results[1]
            return result
        
        return None
    except:
        return None

async def run_scanner_job(active_timeframes):
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 🔄 Memulai Scan: {active_timeframes}")
    
    exchange = ccxt.binance({
        'apiKey': BINANCE_API_KEY, 'secret': BINANCE_SECRET_KEY,
        'enableRateLimit': False, 'options': {'defaultType': 'future'}
    })

    try:
        # 1. AMBIL TOP 400 VOLUME
        symbols = await get_top_volume_symbols(exchange)
        if not symbols: return

        # 2. SCAN SIGNAL
        for tf in active_timeframes:
            print(f"📊 Scanning {tf} ({len(symbols)} coins)...")
            for i in range(0, len(symbols), BATCH_SIZE):
                batch = symbols[i : i + BATCH_SIZE]
                tasks = [process_coin(exchange, sym, tf) for sym in batch]
                results = await asyncio.gather(*tasks)

                for res in results:
                    if res:
                        symbol = res['symbol']
                        price = res['data']['close']
                        tf_res = res['tf']
                        signal_side = res['side']
                        signal_type = res['type']
                        
                        # Data Indikator
                        rsi_info = res['rsi']
                        macd_info = res['macd_status']
                        vol_info = res['vol_status']
                        natr_1m = res['natr_1m']
                        
                        # Status NATR 5m
                        natr_5m = res['natr_5m']
                        status_5m = "Normal"
                        if natr_5m > 0.8: status_5m = "High Vol"
                        elif natr_5m < 0.2: status_5m = "Low Vol"

                        # Header & Caption
                        header = "🚀" if "LONG" in signal_side else "🔻"
                        if signal_type == 'VOLUME_SPIKE': header = "⚡"
                        
                        msg_caption = (
                            f"{header} **SIGNAL {signal_side}**\n\n"
                            f"🪙 `#{symbol.replace('/','')}`\n"
                            f"⏱ TF: `{tf_res}` | 💵 ${price}\n\n"
                            f"📊 **Volume:** {vol_info}\n"
                            f"📈 **RSI:** {rsi_info}\n"
                            f"📉 **MACD:** {macd_info}\n\n"
                            f"🌪 **NATR:**\n1m: `{natr_1m:.2f}%` | 5m: `{natr_5m:.2f}%`\n\n"
                            f"📋 Trigger: {signal_type.replace('_', ' ')}"
                        )

                        print(f"   [SIGNAL] {tf_res} {symbol} ({vol_info})")
                        
                        # Chart Title Info
                        chart_extra = f"| Vol: {vol_info}"
                        chart_file = generate_chart(res['df'], symbol, tf_res, chart_extra)
                        
                        if chart_file:
                            send_photo_sync(msg_caption, chart_file)
                            if os.path.exists(chart_file): os.remove(chart_file)
                        else:
                            send_telegram_sync(msg_caption)

                await asyncio.sleep(DELAY_BATCH)
            
    finally:
        await exchange.close()
        clean_old_memory()
    print(f"✅ Selesai Scan {active_timeframes}")

async def main_scheduler():
    print("=== BOT FILTER ABNORMAL (High/Low Vol Only) ===")
    
    while True:
        now = datetime.now()
        minutes_to_next = 5 - (now.minute % 5)
        next_run = now + timedelta(minutes=minutes_to_next)
        next_run = next_run.replace(second=0, microsecond=0)
        scheduled_time = next_run + timedelta(seconds=15)
        
        seconds_to_wait = (scheduled_time - datetime.now()).total_seconds()
        if seconds_to_wait < 0: seconds_to_wait += 300
            
        print(f"\n⏳ Menunggu {int(seconds_to_wait)} detik...")
        await asyncio.sleep(seconds_to_wait)
        
        run_minute = next_run.minute
        
        active_tfs = ['5m']
        if run_minute % 15 == 0: active_tfs.append('15m')
        if run_minute % 30 == 0: active_tfs.append('30m')
            
        await run_scanner_job(active_tfs)

if __name__ == "__main__":
    try:
        asyncio.run(main_scheduler())
    except KeyboardInterrupt:
        print("\nBot Stopped")


