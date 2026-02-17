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


# Timeframe Scalping
LIMIT_CANDLES = 100
TOP_COINS = 300       
BATCH_SIZE = 20       
DELAY_BATCH = 0.5     

# ================= MEMORY SYSTEM (ANTI-SPAM) =================
# Set ini akan menyimpan ID unik dari sinyal yang sudah dikirim
# Format: "{SYMBOL}_{TIMEFRAME}_{TIMESTAMP}_{TYPE}"
SENT_SIGNALS = set()

def is_signal_already_sent(symbol, timeframe, candle_timestamp, signal_type):
    """Cek apakah sinyal untuk candle ini sudah pernah dikirim"""
    signal_id = f"{symbol}_{timeframe}_{candle_timestamp}_{signal_type}"
    if signal_id in SENT_SIGNALS:
        return True
    return False

def mark_signal_as_sent(symbol, timeframe, candle_timestamp, signal_type):
    """Tandai sinyal ini sebagai sudah dikirim"""
    signal_id = f"{symbol}_{timeframe}_{candle_timestamp}_{signal_type}"
    SENT_SIGNALS.add(signal_id)
    
    # Optional: Print log untuk debug
    # print(f"[MEMORY] Saved: {signal_id}")

def clean_old_memory():
    """Membersihkan memori jika sudah terlalu penuh (misal > 1000 data)"""
    if len(SENT_SIGNALS) > 5000:
        SENT_SIGNALS.clear()
        print("[SYSTEM] Memori sinyal dibersihkan untuk menghemat RAM.")

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

def generate_chart(df, symbol, timeframe):
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
        mpf.plot(plot_df, type='candle', style=s, addplot=ap, 
                 title=f"{symbol} ({timeframe})",
                 savefig=dict(fname=filename, dpi=80, bbox_inches='tight'), volume=False)
        return filename
    except:
        return None

# ================= CORE LOGIC =================

async def get_top_futures_symbols(exchange):
    try:
        markets = await exchange.load_markets()
        tickers = await exchange.fetch_tickers()
        futures = []
        for symbol, data in tickers.items():
            if symbol in markets and markets[symbol].get('quote') == 'USDT' and markets[symbol].get('active'):
                futures.append({'symbol': symbol, 'volume': data.get('quoteVolume', 0)})
        futures.sort(key=lambda x: x['volume'], reverse=True)
        return [x['symbol'] for x in futures[:TOP_COINS]]
    except:
        return []

async def process_coin(exchange, symbol, timeframe):
    try:
        bars = await exchange.fetch_ohlcv(symbol, timeframe, limit=LIMIT_CANDLES)
        if not bars or len(bars) < 35: return None

        df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')

        df['MA5'] = df['close'].rolling(window=5).mean()
        df['MA10'] = df['close'].rolling(window=10).mean()
        df['MA30'] = df['close'].rolling(window=30).mean()

        # Ambil Candle Close (Index -2) & Sebelumnya (Index -3)
        last_closed = df.iloc[-2]
        prev_closed = df.iloc[-3]
        
        # Ambil Timestamp Candle sebagai ID Unik
        candle_ts = last_closed['timestamp'].strftime('%Y-%m-%d %H:%M:%S')
        
        result = {
            'symbol': symbol, 
            'alerts': [], 
            'data': last_closed, 
            'df': df, 
            'tf': timeframe,
            'ts': candle_ts
        }
        has_alert = False

        # --- LOGIKA 1: GOLDEN CROSS ---
        # (Event Cross: Sebelumnya Tidak, Sekarang Ya)
        if prev_closed['MA5'] <= prev_closed['MA10'] and last_closed['MA5'] > last_closed['MA10']:
            # Cek Memory Dulu!
            if not is_signal_already_sent(symbol, timeframe, candle_ts, 'GOLDEN_CROSS'):
                result['alerts'].append('GOLDEN_CROSS')
                has_alert = True
                mark_signal_as_sent(symbol, timeframe, candle_ts, 'GOLDEN_CROSS')

        # --- LOGIKA 2: MA30 TOUCH (Skip 5m) ---
        if timeframe != '5m':
            if last_closed['low'] <= last_closed['MA30'] <= last_closed['high']:
                # Cek Memory Dulu!
                if not is_signal_already_sent(symbol, timeframe, candle_ts, 'MA30_TOUCH'):
                    result['alerts'].append('MA30_TOUCH')
                    has_alert = True
                    mark_signal_as_sent(symbol, timeframe, candle_ts, 'MA30_TOUCH')

        return result if has_alert else None

    except:
        return None

async def run_scanner_job(active_timeframes):
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 🔄 Memulai Scan: {active_timeframes}")
    
    exchange = ccxt.binance({
        'apiKey': BINANCE_API_KEY,
        'secret': BINANCE_SECRET_KEY,
        'enableRateLimit': False,
        'options': {'defaultType': 'future'}
    })

    try:
        symbols = await get_top_futures_symbols(exchange)
        if not symbols: return

        for tf in active_timeframes:
            print(f"📊 Scanning {tf} ({len(symbols)} coins)...")
            
            for i in range(0, len(symbols), BATCH_SIZE):
                batch = symbols[i : i + BATCH_SIZE]
                tasks = [process_coin(exchange, sym, tf) for sym in batch]
                results = await asyncio.gather(*tasks)

                for res in results:
                    if res and res['alerts']:
                        symbol = res['symbol']
                        price = res['data']['close']
                        tf_res = res['tf']
                        
                        if 'GOLDEN_CROSS' in res['alerts']:
                            print(f"   [SIGNAL NEW] {tf_res} GC: {symbol}")
                            chart_file = generate_chart(res['df'], symbol, tf_res)
                            caption = f"🚀 **GOLDEN CROSS ({tf_res})** 🚀\n\n#{symbol.replace('/','')}\nPrice: {price}\nTF: {tf_res}\nMA5 Cross UP MA10"
                            if chart_file:
                                send_photo_sync(caption, chart_file)
                                if os.path.exists(chart_file): os.remove(chart_file)
                            else:
                                send_telegram_sync(caption)

                        if 'MA30_TOUCH' in res['alerts']:
                            print(f"   [SIGNAL NEW] {tf_res} MA30: {symbol}")
                            chart_file = generate_chart(res['df'], symbol, tf_res)
                            caption = f"⚠️ **MA 30 TOUCH ({tf_res})** ⚠️\n\n#{symbol.replace('/','')}\nPrice: {price}\nTF: {tf_res}\nCandle Touched MA30"
                            if chart_file:
                                send_photo_sync(caption, chart_file)
                                if os.path.exists(chart_file): os.remove(chart_file)
                            else:
                                send_telegram_sync(caption)

                await asyncio.sleep(DELAY_BATCH)
            
    finally:
        await exchange.close()
        clean_old_memory() # Bersihkan memori lama
    print(f"✅ Selesai Scan {active_timeframes}")

async def main_scheduler():
    print("=== BOT SMART SCALPING (ANTI-DUPLICATE) ===")
    
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


