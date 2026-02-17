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


# Konfigurasi Trading
LIMIT_CANDLES = 100
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
        # Split pesan panjang (Telegram max 4096 char)
        if len(message) > 4000:
            for x in range(0, len(message), 4000):
                requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message[x:x+4000], "parse_mode": "Markdown"}, timeout=10)
        else:
            requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}, timeout=10)
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

def generate_chart(df, symbol, timeframe, side):
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
        title_text = f"{symbol} ({timeframe}) - {side}"
        mpf.plot(plot_df, type='candle', style=s, addplot=ap, title=title_text,
                 savefig=dict(fname=filename, dpi=80, bbox_inches='tight'), volume=False)
        return filename
    except:
        return None

# ================= INDICATORS & MOVERS =================

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

async def get_momentum_symbols(exchange):
    """
    LOGIKA BARU: Mengambil Top 50 Gainers & Top 50 Losers
    """
    print("🔍 Mengambil Top 50 Gainers & Top 50 Losers...")
    try:
        tickers = await exchange.fetch_tickers()
        valid_tickers = []
        
        for symbol, data in tickers.items():
            # Filter USDT Futures & Aktif
            if '/USDT' in symbol and data.get('percentage') is not None and data.get('active') != False:
                 valid_tickers.append({
                     'symbol': symbol,
                     'change': data['percentage'],
                     'price': data['last']
                 })
        
        # Sortir berdasarkan Persentase Perubahan (Tinggi ke Rendah)
        valid_tickers.sort(key=lambda x: x['change'], reverse=True)
        
        # Ambil 50 Teratas (Gainers)
        top_gainers = valid_tickers[:50]
        # Ambil 50 Terbawah (Losers)
        top_losers = valid_tickers[-50:]
        
        # Gabungkan List (Total 100 Koin)
        target_list = top_gainers + top_losers
        
        # Ambil simbolnya saja
        target_symbols = [x['symbol'] for x in target_list]
        
        print(f"✅ Target Scan: {len(target_symbols)} Koin (Momentum).")
        return target_symbols, top_gainers, top_losers
        
    except Exception as e:
        print(f"Error Momentum Symbols: {e}")
        return [], [], []

async def report_market_movers(top_gainers, top_losers):
    """Kirim Laporan ke Telegram"""
    try:
        # --- KIRIM PESAN GAINERS ---
        msg_gainers = "🚀 **TOP 50 GAINERS (24H)** 🚀\nScan Target:\n\n"
        for i, coin in enumerate(top_gainers, 1):
            msg_gainers += f"{i}. `{coin['symbol'].replace('/','')}`: +{coin['change']:.2f}% (${coin['price']})\n"
        send_telegram_sync(msg_gainers)
        
        await asyncio.sleep(1)

        # --- KIRIM PESAN LOSERS ---
        msg_losers = "🩸 **TOP 50 LOSERS (24H)** 🩸\nScan Target:\n\n"
        for i, coin in enumerate(reversed(top_losers), 1):
            msg_losers += f"{i}. `{coin['symbol'].replace('/','')}`: {coin['change']:.2f}% (${coin['price']})\n"
        send_telegram_sync(msg_losers)
        
    except Exception as e:
        print(f"Error Report: {e}")

# ================= CORE LOGIC =================

async def process_coin(exchange, symbol, timeframe):
    try:
        bars = await exchange.fetch_ohlcv(symbol, timeframe, limit=LIMIT_CANDLES)
        if not bars or len(bars) < 35: return None

        df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')

        df['MA5'] = df['close'].rolling(window=5).mean()
        df['MA10'] = df['close'].rolling(window=10).mean()
        df['MA30'] = df['close'].rolling(window=30).mean()

        last_closed = df.iloc[-2]
        prev_closed = df.iloc[-3]
        candle_ts = last_closed['timestamp'].strftime('%Y-%m-%d %H:%M:%S')
        
        result = {
            'symbol': symbol, 'type': None, 'side': None,
            'data': last_closed, 'df': df, 'tf': timeframe, 'ts': candle_ts,
            'natr_1m': 0.0, 'natr_5m': 0.0
        }
        has_alert = False

        # LOGIKA 1: GOLDEN CROSS
        if prev_closed['MA5'] <= prev_closed['MA10'] and last_closed['MA5'] > last_closed['MA10']:
            if not is_signal_already_sent(symbol, timeframe, candle_ts, 'GOLDEN_CROSS'):
                result['type'] = 'GOLDEN_CROSS'
                result['side'] = 'LONG 🟢'
                has_alert = True
                mark_signal_as_sent(symbol, timeframe, candle_ts, 'GOLDEN_CROSS')

        # LOGIKA 2: MA30 TOUCH (Skip 5m)
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

        if has_alert:
            task1 = calculate_natr(exchange, symbol, '1m', 30)
            task2 = calculate_natr(exchange, symbol, '5m', 14)
            natr_results = await asyncio.gather(task1, task2)
            result['natr_1m'] = natr_results[0]
            result['natr_5m'] = natr_results[1]
            return result
        
        return None
    except:
        return None

async def run_scanner_job(active_timeframes, do_market_report=False):
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 🔄 Memulai Scan: {active_timeframes}")
    
    exchange = ccxt.binance({
        'apiKey': BINANCE_API_KEY, 'secret': BINANCE_SECRET_KEY,
        'enableRateLimit': False, 'options': {'defaultType': 'future'}
    })

    try:
        # 1. AMBIL DATA MOMENTUM (Gainers/Losers)
        # Koin inilah yang akan discan untuk mencari Golden Cross/MA Touch
        symbols, top_gainers, top_losers = await get_momentum_symbols(exchange)
        
        if not symbols: 
            print("Gagal mengambil data momentum.")
            return

        # 2. KIRIM LAPORAN (Jika jadwalnya tiba)
        if do_market_report:
            await report_market_movers(top_gainers, top_losers)

        # 3. SCAN SINYAL (Hanya pada koin-koin momentum tersebut)
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
                        natr_1m = res['natr_1m']
                        natr_5m = res['natr_5m']
                        
                        status_5m = "Normal"
                        if natr_5m > 0.8: status_5m = "High Vol"
                        elif natr_5m < 0.2: status_5m = "Low Vol"

                        header = "🚀" if "LONG" in signal_side else "🔻"
                        msg_caption = (
                            f"{header} **SIGNAL {signal_side}**\n\n"
                            f"🪙 `#{symbol.replace('/','')}`\n"
                            f"⏱ TF: `{tf_res}` | 💵 ${price}\n\n"
                            f"📊 **NATR:**\n1m/30: `{natr_1m:.3f}%`\n5m/14: `{natr_5m:.3f}%` ({status_5m})\n\n"
                            f"📋 Info: {res['type'].replace('_', ' ')}"
                        )

                        print(f"   [SIGNAL] {tf_res} {symbol} -> {signal_side}")
                        chart_file = generate_chart(res['df'], symbol, tf_res, signal_side)
                        
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
    print("=== BOT MOMENTUM SCAN (Gainers/Losers) ===")
    
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
        
        # Kirim laporan Top 50 setiap 1 jam (menit 00)
        should_report_market = (run_minute == 0)
            
        await run_scanner_job(active_tfs, do_market_report=should_report_market)

if __name__ == "__main__":
    try:
        asyncio.run(main_scheduler())
    except KeyboardInterrupt:
        print("\nBot Stopped")


