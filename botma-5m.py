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
# Bot akan otomatis memilih timeframe mana yang discan berdasarkan jam saat ini
ALL_TIMEFRAMES = ['5m', '15m', '30m'] 
LIMIT_CANDLES = 100
TOP_COINS = 300       # Scan 300 koin
BATCH_SIZE = 20       # Batch size lebih kecil agar cepat
DELAY_BATCH = 0.5     # Jeda cepat

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
        plot_df = df.tail(50).copy()
        plot_df.set_index('timestamp', inplace=True)
        mc = mpf.make_marketcolors(up='#2ebd85', down='#f6465d', inherit=True)
        s = mpf.make_mpf_style(marketcolors=mc, gridstyle=':', y_on_right=True)
        ap = [
            mpf.make_addplot(plot_df['MA5'], color='cyan', width=1),
            mpf.make_addplot(plot_df['MA10'], color='orange', width=1),
            mpf.make_addplot(plot_df['MA30'], color='purple', width=2),
        ]
        mpf.plot(plot_df, type='candle', style=s, addplot=ap, 
                 title=f"{symbol} ({timeframe}) - Signal",
                 savefig=dict(fname=filename, dpi=80, bbox_inches='tight'), volume=False)
        return filename
    except:
        return None

# ================= CORE ASYNC LOGIC =================

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

        # Ambil Candle Close (Index -2)
        last_closed = df.iloc[-2]
        prev_closed = df.iloc[-3]
        
        result = {'symbol': symbol, 'alerts': [], 'data': last_closed, 'df': df, 'tf': timeframe}
        has_alert = False

        # 1. Golden Cross
        if prev_closed['MA5'] <= prev_closed['MA10'] and last_closed['MA5'] > last_closed['MA10']:
            result['alerts'].append('GOLDEN_CROSS')
            has_alert = True

        # 2. MA30 Touch
        if last_closed['low'] <= last_closed['MA30'] <= last_closed['high']:
            result['alerts'].append('MA30_TOUCH')
            has_alert = True

        return result if has_alert else None

    except:
        return None

async def run_scanner_job(active_timeframes):
    """Scan hanya timeframe yang aktif pada menit ini"""
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

        # Loop Timeframe yang aktif saja
        for tf in active_timeframes:
            print(f"📊 Scanning {tf} ({len(symbols)} coins)...")
            
            # Batch Processing
            for i in range(0, len(symbols), BATCH_SIZE):
                batch = symbols[i : i + BATCH_SIZE]
                coroutines = [process_coin(exchange, sym, tf) for sym in batch]
                results = await asyncio.gather(*coroutines)

                for res in results:
                    if res:
                        symbol = res['symbol']
                        price = res['data']['close']
                        tf_res = res['tf']
                        
                        if 'GOLDEN_CROSS' in res['alerts']:
                            print(f"   [SIGNAL {tf_res}] GC: {symbol}")
                            chart_file = generate_chart(res['df'], symbol, tf_res)
                            caption = f"🚀 **GOLDEN CROSS ({tf_res})** 🚀\n#{symbol.replace('/','')}\nPrice: {price}\nTF: {tf_res}\nMA5 Cross UP MA10"
                            if chart_file:
                                send_photo_sync(caption, chart_file)
                                if os.path.exists(chart_file): os.remove(chart_file)

                        if 'MA30_TOUCH' in res['alerts']:
                            print(f"   [SIGNAL {tf_res}] MA30: {symbol}")
                            chart_file = generate_chart(res['df'], symbol, tf_res)
                            caption = f"⚠️ **MA 30 TOUCH ({tf_res})** ⚠️\n#{symbol.replace('/','')}\nPrice: {price}\nTF: {tf_res}\nMA30 Touched"
                            if chart_file:
                                send_photo_sync(caption, chart_file)
                                if os.path.exists(chart_file): os.remove(chart_file)

                # Jeda kecil antar batch
                await asyncio.sleep(DELAY_BATCH)
            
    finally:
        await exchange.close()
    print(f"✅ Selesai Scan {active_timeframes}")

async def main_scheduler():
    print("=== BOT SCALPING (5m, 15m, 30m) STARTED ===")
    
    while True:
        now = datetime.now()
        
        # Hitung waktu ke kelipatan 5 menit berikutnya
        # Contoh: 12:02 -> Next 12:05
        # Contoh: 12:06 -> Next 12:10
        next_run_minute = (now.minute // 5 + 1) * 5
        next_run = now.replace(minute=0, second=0, microsecond=0) + timedelta(minutes=next_run_minute)
        
        # Buffer 10 detik agar candle close sempurna
        scheduled_time = next_run + timedelta(seconds=10)
        
        seconds_to_wait = (scheduled_time - now).total_seconds()
        print(f"\n⏳ Standby {int(seconds_to_wait)} detik sampai {scheduled_time.strftime('%H:%M:%S')}...")
        
        await asyncio.sleep(seconds_to_wait)
        
        # --- LOGIKA PENENTUAN TIMEFRAME ---
        # Saat bangun, cek kita ada di menit berapa
        current_minute = next_run.minute
        
        active_tfs = []
        
        # 5m selalu discan setiap kelipatan 5 menit
        active_tfs.append('5m')
        
        # 15m discan jika menit 0, 15, 30, 45
        if current_minute % 15 == 0:
            active_tfs.append('15m')
            
        # 30m discan jika menit 0, 30
        if current_minute % 30 == 0:
            active_tfs.append('30m')
            
        # Jalankan Scan
        await run_scanner_job(active_tfs)

if __name__ == "__main__":
    try:
        asyncio.run(main_scheduler())
    except KeyboardInterrupt:
        print("\nBot Stopped")


