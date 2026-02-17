import asyncio
import ccxt.async_support as ccxt  # Library Async
import pandas as pd
import mplfinance as mpf
import requests
import os
import time
from datetime import datetime, timedelta

# ================= KONFIGURASI =================
# Pastikan Environment Variable sudah diset
BINANCE_API_KEY = os.environ.get('BINANCE_API_KEY', 'fZwDMOfBL6rDU9jfUQox64fUAb2RSN48myxMPUGDAINYjmLdqJmUFhVRWLqlsX97')
BINANCE_SECRET_KEY = os.environ.get('BINANCE_API_SECRET', 'FmZNNbIOWIAddxVoLcNowLNW379E6gxyM85Bvy3QzlRMtK1eMApJp6vJtpGHWdWB')
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', '8562793193:AAHDulfzVhhnuPfNfy4Zk6ONBNSNbGwVJ8c')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '-1003819540522')


# Setting Scanner Multi Timeframe
TIMEFRAMES = ['4h', '1d', '1w']  # Scan 4 Jam, 1 Hari, dan 1 Minggu
LIMIT_CANDLES = 100    # Jumlah candle untuk analisa
TOP_COINS = 400        # Scan 400 koin teratas
BATCH_SIZE = 25        # Memproses 25 koin sekaligus (Async Batch)
DELAY_BATCH = 1.0      # Jeda antar batch

# ================= FUNGSI BANTUAN (UTILITIES) =================

def send_telegram_sync(message):
    if not TELEGRAM_TOKEN: return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}, timeout=5)
    except Exception as e:
        print(f"[ERROR] Gagal kirim TG: {e}")

def send_photo_sync(caption, filepath):
    if not TELEGRAM_TOKEN: return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
        with open(filepath, 'rb') as img:
            requests.post(url, files={'photo': img}, data={'chat_id': TELEGRAM_CHAT_ID, 'caption': caption}, timeout=10)
    except Exception as e:
        print(f"[ERROR] Gagal kirim Foto: {e}")

def generate_chart(df, symbol, timeframe, vol_status):
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
                 title=f"{symbol} ({timeframe}) - {vol_status}",
                 savefig=dict(fname=filename, dpi=80, bbox_inches='tight'), volume=False)
        return filename
    except Exception as e:
        print(f"Error making chart {symbol}: {e}")
        return None

# ================= INDICATORS CALCULATION =================

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

# ================= LOGIKA UTAMA (ASYNC) =================

async def get_top_futures_symbols(exchange):
    try:
        markets = await exchange.load_markets()
        tickers = await exchange.fetch_tickers()
        
        futures = []
        for symbol, data in tickers.items():
            if symbol in markets and markets[symbol].get('quote') == 'USDT' and markets[symbol].get('active'):
                futures.append({'symbol': symbol, 'volume': data.get('quoteVolume', 0)})
        
        futures.sort(key=lambda x: x['volume'], reverse=True)
        top_list = [x['symbol'] for x in futures[:TOP_COINS]]
        return top_list
    except Exception as e:
        print(f"Error fetch markets: {e}")
        return []

async def process_coin(exchange, symbol, timeframe):
    try:
        bars = await exchange.fetch_ohlcv(symbol, timeframe, limit=LIMIT_CANDLES)
        if not bars or len(bars) < 35: return None

        df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')

        # Indikator
        df['MA5'] = df['close'].rolling(window=5).mean()
        df['MA10'] = df['close'].rolling(window=10).mean()
        df['MA30'] = df['close'].rolling(window=30).mean()
        df['RSI'] = calculate_rsi(df)
        df['MACD'], df['MACD_SIGNAL'] = calculate_macd(df)
        df['VolMA'] = df['volume'].rolling(window=20).mean() # Rata-rata volume 20 candle

        last_closed = df.iloc[-2]
        prev_closed = df.iloc[-3]
        
        # --- FILTER UTAMA: VOLUME CHECK ---
        curr_vol = last_closed['volume']
        avg_vol = last_closed['VolMA']
        
        if avg_vol == 0: return None
        
        vol_ratio = curr_vol / avg_vol
        
        # LOGIKA MARKET TIDAK NORMAL
        is_high_vol = vol_ratio >= 1.5  # Volume > 1.5x Rata-rata (Ledakan)
        is_low_vol = vol_ratio <= 0.5   # Volume < 0.5x Rata-rata (Kering)
        
        # SANGAT PENTING: Jika volume NORMAL (antara 0.5x - 1.5x), SKIP KOIN INI
        if not (is_high_vol or is_low_vol):
            return None 
            
        # Label Volume untuk Caption
        vol_status_label = "🔥 HIGH VOLUME" if is_high_vol else "❄️ LOW VOLUME (DRY)"
        vol_desc = f"{vol_ratio:.1f}x Avg"

        result = {
            'symbol': symbol, 
            'alerts': [], 
            'data': last_closed, 
            'df': df, 
            'tf': timeframe,
            'vol_status': vol_status_label,
            'vol_desc': vol_desc
        }
        
        # --- ANALISA LOGIKA SIGNAL (Hanya jika lolos filter volume) ---
        
        # A. Status RSI
        rsi_val = last_closed['RSI']
        rsi_status = "Neutral"
        if rsi_val > 70: rsi_status = "OVERBOUGHT 🔴"
        elif rsi_val < 30: rsi_status = "OVERSOLD 🟢"

        # B. Status MACD
        macd_val = last_closed['MACD']
        sig_val = last_closed['MACD_SIGNAL']
        macd_status = "Bullish" if macd_val > sig_val else "Bearish"

        result['tech_info'] = {
            'rsi': rsi_val,
            'rsi_status': rsi_status,
            'macd': macd_status,
        }

        # Trigger 1: GOLDEN CROSS
        if prev_closed['MA5'] <= prev_closed['MA10'] and last_closed['MA5'] > last_closed['MA10']:
            result['alerts'].append('GOLDEN_CROSS')

        # Trigger 2: MA30 TOUCH
        if last_closed['low'] <= last_closed['MA30'] <= last_closed['high']:
            result['alerts'].append('MA30_TOUCH')
            
        # Trigger 3: EXTREME VOLUME ONLY (Jika tidak ada cross/touch, tapi volume sangat aneh)
        # Misal volume naik 3x lipat, kita tetap kirim info
        if vol_ratio >= 3.0:
            result['alerts'].append('EXTREME_VOL')

        # Hanya kembalikan result jika ada alert
        if len(result['alerts']) > 0:
            return result
        
        return None

    except Exception as e:
        return None

async def run_scanner_job():
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 🔄 Memulai Scan (FILTER: MARKET ABNORMAL ONLY)...")
    
    exchange = ccxt.binance({
        'apiKey': BINANCE_API_KEY,
        'secret': BINANCE_SECRET_KEY,
        'enableRateLimit': False, 
        'options': {'defaultType': 'future'}
    })

    try:
        symbols = await get_top_futures_symbols(exchange)
        if not symbols:
            await exchange.close()
            return

        for tf in TIMEFRAMES:
            print(f"\n📊 Scanning Timeframe: {tf} ...")
            signals_found = 0
            
            for i in range(0, len(symbols), BATCH_SIZE):
                batch = symbols[i : i + BATCH_SIZE]
                print(f"   Batch {i+1}-{min(i+BATCH_SIZE, len(symbols))} ({tf})...", end='\r')
                
                coroutines = [process_coin(exchange, sym, tf) for sym in batch]
                results = await asyncio.gather(*coroutines)

                for res in results:
                    if res:
                        symbol = res['symbol']
                        price = res['data']['close']
                        tech = res['tech_info']
                        current_tf = res['tf']
                        vol_stat = res['vol_status']
                        vol_desc = res['vol_desc']
                        
                        # Base Caption untuk semua alert
                        base_caption = (
                            f"#{symbol.replace('/','')} ({current_tf})\n"
                            f"💰 Price: {price}\n"
                            f"📊 **Volume: {vol_stat}** ({vol_desc})\n\n"
                            f"📉 **Indikator:**\n"
                            f"• RSI: {tech['rsi']:.1f} ({tech['rsi_status']})\n"
                            f"• MACD: {tech['macd']}\n"
                        )

                        # Kirim Alert sesuai tipe
                        if 'GOLDEN_CROSS' in res['alerts']:
                            signals_found += 1
                            print(f"\n   [{vol_stat}] {symbol} Golden Cross")
                            chart_file = generate_chart(res['df'], symbol, current_tf, vol_stat)
                            caption = f"🚀 **GOLDEN CROSS (+Vol Alert)** 🚀\n\n" + base_caption
                            if chart_file:
                                send_photo_sync(caption, chart_file)
                                if os.path.exists(chart_file): os.remove(chart_file)

                        elif 'MA30_TOUCH' in res['alerts']:
                            signals_found += 1
                            print(f"\n   [{vol_stat}] {symbol} MA30 Touch")
                            chart_file = generate_chart(res['df'], symbol, current_tf, vol_stat)
                            caption = f"⚠️ **MA 30 TOUCH (+Vol Alert)** ⚠️\n\n" + base_caption
                            if chart_file:
                                send_photo_sync(caption, chart_file)
                                if os.path.exists(chart_file): os.remove(chart_file)
                                
                        elif 'EXTREME_VOL' in res['alerts']:
                             # Jika volume 3x lipat tapi tidak ada Golden Cross/MA30, tetap info
                            signals_found += 1
                            print(f"\n   [EXTREME] {symbol} Vol Spike Only")
                            chart_file = generate_chart(res['df'], symbol, current_tf, vol_stat)
                            caption = f"⚡ **EXTREME VOLUME SPIKE** ⚡\n\n" + base_caption
                            if chart_file:
                                send_photo_sync(caption, chart_file)
                                if os.path.exists(chart_file): os.remove(chart_file)

                await asyncio.sleep(DELAY_BATCH)
            
            print(f"\n   ✅ Selesai {tf}. Sinyal ditemukan: {signals_found}")

    finally:
        await exchange.close()

# ================= LOOP UTAMA =================

async def main_scheduler():
    print("=== BOT FILTER VOLUME (HIGH/LOW ONLY) STARTED ===")
    
    await run_scanner_job()
    
    while True:
        now = datetime.now()
        current_hour = now.hour
        next_hour_target = (current_hour // 4 + 1) * 4
        
        if next_hour_target >= 24:
            next_run = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        else:
            next_run = now.replace(hour=next_hour_target, minute=0, second=0, microsecond=0)
        
        scheduled_time = next_run + timedelta(seconds=15)
        seconds_to_wait = (scheduled_time - now).total_seconds()
        
        print(f"\n⏳ Menunggu {int(seconds_to_wait/60)} menit sampai {scheduled_time.strftime('%H:%M:%S')}...")
        
        await asyncio.sleep(seconds_to_wait)
        
        print("\n⏰ WAKTU SCAN TIBA! Memulai analisa...")
        await run_scanner_job()

if __name__ == "__main__":
    try:
        asyncio.run(main_scheduler())
    except KeyboardInterrupt:
        print("\nBot Stopped by User")



