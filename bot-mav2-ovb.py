import asyncio
import ccxt.async_support as ccxt  # Library Async
import pandas as pd
import mplfinance as mpf
import requests
import os
import time
import numpy as np
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor

# ================= KONFIGURASI =================
BINANCE_API_KEY = os.environ.get('BINANCE_API_KEY', 'fZwDMOfBL6rDU9jfUQox64fUAb2RSN48myxMPUGDAINYjmLdqJmUFhVRWLqlsX97')
BINANCE_SECRET_KEY = os.environ.get('BINANCE_API_SECRET', 'FmZNNbIOWIAddxVoLcNowLNW379E6gxyM85Bvy3QzlRMtK1eMApJp6vJtpGHWdWB')
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', '8562793193:AAHDulfzVhhnuPfNfy4Zk6ONBNSNbGwVJ8c')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '-1003819540522')

# Setting Scanner Multi Timeframe
TIMEFRAMES = ['15m', '1h', '4h']
LIMIT_CANDLES = 100
BATCH_SIZE = 50        # NAIKKAN KE 50 AGAR LEBIH CEPAT
DELAY_BATCH = 0.5      # KURANGI DELAY AGAR LEBIH NGEBUT

# ThreadPool untuk fungsi blocking (kirim gambar/generate chart)
executor = ThreadPoolExecutor(max_workers=5)

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
    finally:
        # Hapus file setelah mencoba mengirim (berhasil atau gagal)
        if os.path.exists(filepath):
            try:
                os.remove(filepath)
            except:
                pass

async def send_photo_async(caption, filepath):
    """Wrapper Async untuk pengiriman foto agar tidak memblokir scanning"""
    loop = asyncio.get_running_loop()
    # Jalankan di thread terpisah agar bot tidak berhenti scan saat upload
    await loop.run_in_executor(executor, send_photo_sync, caption, filepath)

def generate_chart(df, symbol, timeframe, vol_status, oi_data=None):
    filename = f"chart_{symbol.replace('/', '')}_{timeframe}_{int(time.time())}.png"
    try:
        plot_df = df.tail(60).copy()
        if not isinstance(plot_df.index, pd.DatetimeIndex):
            plot_df.set_index('timestamp', inplace=True)
        
        mc = mpf.make_marketcolors(up='#2ebd85', down='#f6465d', inherit=True)
        s = mpf.make_mpf_style(marketcolors=mc, gridstyle=':', y_on_right=True)
        
        ap = [
            mpf.make_addplot(plot_df['MA5'], color='cyan', width=1, panel=0),
            mpf.make_addplot(plot_df['MA10'], color='orange', width=1, panel=0),
            mpf.make_addplot(plot_df['MA30'], color='purple', width=2, panel=0),
        ]

        if 'OBV' in plot_df.columns:
            ap.append(mpf.make_addplot(plot_df['OBV'], panel=2, color='blue', title='OBV', width=1.5, ylabel='OBV'))

        if oi_data is not None and not oi_data.empty:
            oi_aligned = oi_data.reindex(plot_df.index, method='ffill')
            ap.append(mpf.make_addplot(oi_aligned, panel=3, color='#f1c40f', title='Open Interest', width=1.5, ylabel='OI'))

        mpf.plot(
            plot_df, 
            type='candle', 
            style=s, 
            addplot=ap, 
            title=f"{symbol} ({timeframe}) - {vol_status}",
            savefig=dict(fname=filename, dpi=100, bbox_inches='tight'), 
            volume=True,
            panel_ratios=(4,1,1,1),
            figscale=1.2
        )
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

def calculate_obv(df):
    change = df['close'].diff().fillna(0)
    direction = np.sign(change)
    obv = (direction * df['volume']).cumsum()
    return obv

async def fetch_oi_history(exchange, symbol, timeframe):
    try:
        oi_data = await exchange.fetch_open_interest_history(symbol, timeframe, limit=80)
        if not oi_data: return None
        df_oi = pd.DataFrame(oi_data)
        df_oi['timestamp'] = pd.to_datetime(df_oi['timestamp'], unit='ms')
        df_oi.set_index('timestamp', inplace=True)
        return df_oi['openInterest'] 
    except Exception as e:
        return None

# ================= LOGIKA UTAMA (ASYNC) =================

async def get_all_futures_symbols(exchange):
    try:
        print("   ⬇️ Mengambil seluruh data ticker Futures (All Market)...")
        markets = await exchange.load_markets()
        tickers = await exchange.fetch_tickers()
        
        futures = []
        for symbol, data in tickers.items():
            if symbol in markets and markets[symbol].get('quote') == 'USDT' and markets[symbol].get('active'):
                futures.append({'symbol': symbol, 'volume': data.get('quoteVolume', 0)})
        
        futures.sort(key=lambda x: x['volume'], reverse=True)
        all_symbols = [x['symbol'] for x in futures]
        print(f"   ✅ Ditemukan {len(all_symbols)} pair USDT Futures aktif.")
        return all_symbols
    except Exception as e:
        print(f"Error fetch markets: {e}")
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
        df['RSI'] = calculate_rsi(df)
        df['MACD'], df['MACD_SIGNAL'] = calculate_macd(df)
        df['VolMA'] = df['volume'].rolling(window=20).mean() 
        df['OBV'] = calculate_obv(df)

        last_closed = df.iloc[-2]
        prev_closed = df.iloc[-3]
        
        curr_vol = last_closed['volume']
        avg_vol = last_closed['VolMA']
        
        if avg_vol == 0: return None
        vol_ratio = curr_vol / avg_vol
        
        is_high_vol = vol_ratio >= 1.5
        is_low_vol = vol_ratio <= 0.5
        
        if not (is_high_vol or is_low_vol):
            return None 
            
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
        
        rsi_val = last_closed['RSI']
        rsi_status = "Neutral"
        if rsi_val > 70: rsi_status = "OVERBOUGHT 🔴"
        elif rsi_val < 30: rsi_status = "OVERSOLD 🟢"

        macd_val = last_closed['MACD']
        sig_val = last_closed['MACD_SIGNAL']
        macd_status = "Bullish" if macd_val > sig_val else "Bearish"

        result['tech_info'] = {'rsi': rsi_val, 'rsi_status': rsi_status, 'macd': macd_status}

        if prev_closed['MA5'] <= prev_closed['MA10'] and last_closed['MA5'] > last_closed['MA10']:
            result['alerts'].append('GOLDEN_CROSS')

        if last_closed['low'] <= last_closed['MA30'] <= last_closed['high']:
            result['alerts'].append('MA30_TOUCH')
            
        if vol_ratio >= 3.0:
            result['alerts'].append('EXTREME_VOL')

        if len(result['alerts']) > 0:
            return result
        
        return None

    except Exception:
        return None

async def run_scanner_job():
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 🔄 Memulai Scan Cepat (Batch Size {BATCH_SIZE})...")
    
    exchange = ccxt.binance({
        'apiKey': BINANCE_API_KEY,
        'secret': BINANCE_SECRET_KEY,
        'enableRateLimit': True,
        'options': {'defaultType': 'future'}
    })

    try:
        symbols = await get_all_futures_symbols(exchange)
        if not symbols:
            await exchange.close()
            return

        for tf in TIMEFRAMES:
            print(f"\n📊 Scanning Timeframe: {tf}")
            signals_found = 0
            
            for i in range(0, len(symbols), BATCH_SIZE):
                batch = symbols[i : i + BATCH_SIZE]
                # Indikator progress yang lebih bersih
                print(f"   🚀 Processing {i+1} - {min(i+BATCH_SIZE, len(symbols))} / {len(symbols)} coins...", end='\r')
                
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
                        
                        # --- FETCH OI (Async) ---
                        oi_series = await fetch_oi_history(exchange, symbol, current_tf)
                        last_oi = f"{oi_series.iloc[-1]:,.0f}" if (oi_series is not None and not oi_series.empty) else "N/A"

                        base_caption = (
                            f"#{symbol.replace('/','')} ({current_tf})\n"
                            f"💰 Price: {price}\n"
                            f"📊 **Volume: {vol_stat}** ({vol_desc})\n"
                            f"⚡ **Open Interest:** {last_oi}\n\n"
                            f"📉 **Indikator:**\n"
                            f"• RSI: {tech['rsi']:.1f} ({tech['rsi_status']})\n"
                            f"• MACD: {tech['macd']}\n"
                        )

                        caption_header = ""
                        should_send = False

                        if 'GOLDEN_CROSS' in res['alerts']:
                            signals_found += 1
                            print(f"\n   [{vol_stat}] {symbol} Golden Cross")
                            caption_header = f"🚀 **GOLDEN CROSS (+Vol Alert)** 🚀\n\n"
                            should_send = True

                        elif 'MA30_TOUCH' in res['alerts']:
                            signals_found += 1
                            print(f"\n   [{vol_stat}] {symbol} MA30 Touch")
                            caption_header = f"⚠️ **MA 30 TOUCH (+Vol Alert)** ⚠️\n\n"
                            should_send = True
                                
                        elif 'EXTREME_VOL' in res['alerts']:
                            signals_found += 1
                            print(f"\n   [EXTREME] {symbol} Vol Spike Only")
                            caption_header = f"⚡ **EXTREME VOLUME SPIKE** ⚡\n\n"
                            should_send = True
                        
                        if should_send:
                            # Generate Chart di ThreadPool (supaya tidak berat di event loop)
                            loop = asyncio.get_running_loop()
                            chart_file = await loop.run_in_executor(executor, generate_chart, res['df'], symbol, current_tf, vol_stat, oi_series)
                            
                            final_caption = caption_header + base_caption
                            
                            if chart_file:
                                # Fire and forget: Kirim TG tanpa menunggu selesai (non-blocking)
                                asyncio.create_task(send_photo_async(final_caption, chart_file))

                # Delay antar batch (dikurangi agar lebih cepat)
                await asyncio.sleep(DELAY_BATCH)
            
            print(f"\n   ✅ Selesai {tf}. Sinyal ditemukan: {signals_found}")

    finally:
        await exchange.close()

# ================= LOOP UTAMA =================

async def main_scheduler():
    print("=== BOT FILTER VOLUME + OBV + OI (FAST MODE) STARTED ===")
    
    await run_scanner_job()
    
    while True:
        now = datetime.now()
        minutes_to_next = 15 - (now.minute % 15)
        next_run = (now + timedelta(minutes=minutes_to_next)).replace(second=0, microsecond=0)
        scheduled_time = next_run + timedelta(seconds=15)
        
        seconds_to_wait = (scheduled_time - now).total_seconds()
        if seconds_to_wait < 0:
            seconds_to_wait += 900
            scheduled_time += timedelta(minutes=15)
        
        print(f"\n⏳ Menunggu {int(seconds_to_wait/60)} menit {int(seconds_to_wait%60)} detik sampai {scheduled_time.strftime('%H:%M:%S')}...")
        
        await asyncio.sleep(seconds_to_wait)
        print("\n⏰ WAKTU SCAN TIBA! Memulai analisa...")
        await run_scanner_job()

if __name__ == "__main__":
    try:
        asyncio.run(main_scheduler())
    except KeyboardInterrupt:
        print("\nBot Stopped by User")
