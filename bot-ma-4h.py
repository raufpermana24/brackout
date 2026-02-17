import asyncio
import ccxt.async_support as ccxt  # Library Async
import pandas as pd
import mplfinance as mpf
import requests
import os
import time
from datetime import datetime, timedelta

# ================= KONFIGURASI =================
# Pastikan Environment Variable sudah diset, atau isi default value di parameter kedua
BINANCE_API_KEY = os.environ.get('BINANCE_API_KEY', 'fZwDMOfBL6rDU9jfUQox64fUAb2RSN48myxMPUGDAINYjmLdqJmUFhVRWLqlsX97')
BINANCE_SECRET_KEY = os.environ.get('BINANCE_API_SECRET', 'FmZNNbIOWIAddxVoLcNowLNW379E6gxyM85Bvy3QzlRMtK1eMApJp6vJtpGHWdWB')
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', '8562793193:AAHDulfzVhhnuPfNfy4Zk6ONBNSNbGwVJ8c')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '-1003819540522')


# Setting Scanner Multi Timeframe
TIMEFRAMES = ['4h', '1d', '1w']  # Scan 4 Jam, 1 Hari, dan 1 Minggu
LIMIT_CANDLES = 100    # Jumlah candle untuk analisa
TOP_COINS = 400        # Scan 400 koin teratas
BATCH_SIZE = 25        # Memproses 25 koin sekaligus (Async Batch)
DELAY_BATCH = 1.0      # Jeda antar batch (detik) agar aman dari Ban IP

# ================= FUNGSI BANTUAN (UTILITIES) =================

def send_telegram_sync(message):
    """Kirim pesan teks (Synchronous)"""
    if not TELEGRAM_TOKEN: return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}, timeout=5)
    except Exception as e:
        print(f"[ERROR] Gagal kirim TG: {e}")

def send_photo_sync(caption, filepath):
    """Kirim foto (Synchronous)"""
    if not TELEGRAM_TOKEN: return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
        with open(filepath, 'rb') as img:
            requests.post(url, files={'photo': img}, data={'chat_id': TELEGRAM_CHAT_ID, 'caption': caption}, timeout=10)
    except Exception as e:
        print(f"[ERROR] Gagal kirim Foto: {e}")

def generate_chart(df, symbol, timeframe):
    """Membuat chart png secara lokal"""
    filename = f"chart_{symbol.replace('/', '')}_{timeframe}_{int(time.time())}.png"
    try:
        # Ambil 60 candle terakhir agar chart jelas
        plot_df = df.tail(60).copy()
        plot_df.set_index('timestamp', inplace=True)
        
        # Style Chart Binance (Hijau/Merah)
        mc = mpf.make_marketcolors(up='#2ebd85', down='#f6465d', inherit=True)
        s = mpf.make_mpf_style(marketcolors=mc, gridstyle=':', y_on_right=True)
        
        # Tambah garis MA
        ap = [
            mpf.make_addplot(plot_df['MA5'], color='cyan', width=1),
            mpf.make_addplot(plot_df['MA10'], color='orange', width=1),
            mpf.make_addplot(plot_df['MA30'], color='purple', width=2),
        ]
        
        # Note: Kita tidak menambahkan panel RSI/MACD di gambar agar chart tetap bersih di layar HP
        # Data RSI/MACD akan dikirim via teks caption yang lebih mudah dibaca.
        
        mpf.plot(plot_df, type='candle', style=s, addplot=ap, 
                 title=f"{symbol} ({timeframe}) - Analysis",
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
    """Mengambil daftar 400 koin futures teratas berdasarkan volume"""
    try:
        markets = await exchange.load_markets()
        tickers = await exchange.fetch_tickers()
        
        futures = []
        for symbol, data in tickers.items():
            # Filter hanya USDT Futures yang aktif
            if symbol in markets and markets[symbol].get('quote') == 'USDT' and markets[symbol].get('active'):
                futures.append({'symbol': symbol, 'volume': data.get('quoteVolume', 0)})
        
        # Sort volume terbesar -> terkecil
        futures.sort(key=lambda x: x['volume'], reverse=True)
        top_list = [x['symbol'] for x in futures[:TOP_COINS]]
        return top_list
    except Exception as e:
        print(f"Error fetch markets: {e}")
        return []

async def process_coin(exchange, symbol, timeframe):
    """Proses 1 koin: Fetch -> Calculate Indicators -> Analyze Logic"""
    try:
        bars = await exchange.fetch_ohlcv(symbol, timeframe, limit=LIMIT_CANDLES)
        if not bars or len(bars) < 35: return None

        # 2. DataFrame Processing
        df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')

        # 3. Indikator MA
        df['MA5'] = df['close'].rolling(window=5).mean()
        df['MA10'] = df['close'].rolling(window=10).mean()
        df['MA30'] = df['close'].rolling(window=30).mean()

        # 4. Indikator Tambahan (RSI, MACD, Volume MA)
        df['RSI'] = calculate_rsi(df)
        df['MACD'], df['MACD_SIGNAL'] = calculate_macd(df)
        df['VolMA'] = df['volume'].rolling(window=20).mean() # Rata-rata volume 20 candle

        # 5. Ambil Candle Close Terakhir (Index -2)
        last_closed = df.iloc[-2]
        prev_closed = df.iloc[-3]
        
        result = {'symbol': symbol, 'alerts': [], 'data': last_closed, 'df': df, 'tf': timeframe}
        
        # --- ANALISA LOGIKA SIGNAL ---
        
        # A. Cek Volume Spike (Volume Naik Signifikan)
        # Volume sekarang > 1.5x Rata-rata volume
        is_volume_spike = last_closed['volume'] > (last_closed['VolMA'] * 1.5)
        
        # B. Cek Status RSI (Overbought/Oversold)
        rsi_val = last_closed['RSI']
        rsi_status = "Neutral"
        if rsi_val > 70: rsi_status = "OVERBOUGHT (Jenuh Beli) 🔴"
        elif rsi_val < 30: rsi_status = "OVERSOLD (Jenuh Jual) 🟢"

        # C. Cek Status MACD
        macd_val = last_closed['MACD']
        sig_val = last_closed['MACD_SIGNAL']
        macd_status = "Bullish" if macd_val > sig_val else "Bearish"

        # Simpan info teknikal untuk caption
        result['tech_info'] = {
            'rsi': rsi_val,
            'rsi_status': rsi_status,
            'macd': macd_status,
            'vol_spike': is_volume_spike
        }

        # --- LOGIKA TRIGGER ALERT ---
        
        # Trigger 1: VOLUME SPIKE (Volume Naik Tinggi)
        # Kita jadikan ini sebagai filter utama sesuai request: "kirim signal koin yang volume nya lagi naik"
        if is_volume_spike:
            result['alerts'].append('VOLUME_SPIKE')

        # Trigger 2: GOLDEN CROSS (dengan filter volume opsional)
        if prev_closed['MA5'] <= prev_closed['MA10'] and last_closed['MA5'] > last_closed['MA10']:
            result['alerts'].append('GOLDEN_CROSS')

        # Trigger 3: MA30 TOUCH
        if last_closed['low'] <= last_closed['MA30'] <= last_closed['high']:
            result['alerts'].append('MA30_TOUCH')

        # Hanya kembalikan result jika ada alert
        if len(result['alerts']) > 0:
            return result
        
        return None

    except Exception as e:
        return None

async def run_scanner_job():
    """Menjalankan 1 putaran scan penuh untuk SEMUA timeframe"""
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 🔄 Memulai Scan {TOP_COINS} Koin (Vol, RSI, MACD)...")
    
    exchange = ccxt.binance({
        'apiKey': BINANCE_API_KEY,
        'secret': BINANCE_SECRET_KEY,
        'enableRateLimit': False, 
        'options': {'defaultType': 'future'}
    })

    try:
        symbols = await get_top_futures_symbols(exchange)
        if not symbols:
            print("Gagal mengambil simbol.")
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
                        
                        # Filter: Jika request user "baru kirim signal koin yang volume nya lagi naik",
                        # kita bisa memprioritaskan alert yang memiliki 'VOLUME_SPIKE' atau menggabungkannya.
                        # Di sini saya akan menampilkan semua alert tapi memberikan highlight pada Volume.
                        
                        vol_text = "🔥 **VOLUME SPIKE DETECTED!** 🔥" if tech['vol_spike'] else "Volume: Normal"
                        
                        # Susun Caption Pesan
                        base_caption = (
                            f"#{symbol.replace('/','')} ({current_tf})\n"
                            f"💰 Price: {price}\n"
                            f"{vol_text}\n\n"
                            f"📊 **Indikator:**\n"
                            f"• RSI: {tech['rsi']:.1f} - {tech['rsi_status']}\n"
                            f"• MACD: {tech['macd']}\n"
                        )

                        # Kirim Alert sesuai tipe yang ditemukan
                        if 'VOLUME_SPIKE' in res['alerts']:
                            # Kirim alert khusus jika ada volume spike signifikan + kondisi RSI menarik
                            if tech['rsi'] > 70 or tech['rsi'] < 30: # Filter tambahan biar tidak spam spike biasa
                                signals_found += 1
                                print(f"\n   [VOL+RSI] {symbol} Vol Spike & {tech['rsi_status']}")
                                chart_file = generate_chart(res['df'], symbol, current_tf)
                                caption = f"⚡ **VOLUME & RSI ALERT** ⚡\n\n" + base_caption
                                if chart_file:
                                    send_photo_sync(caption, chart_file)
                                    if os.path.exists(chart_file): os.remove(chart_file)
                        
                        elif 'GOLDEN_CROSS' in res['alerts']:
                            signals_found += 1
                            print(f"\n   [GC] {symbol} Golden Cross")
                            chart_file = generate_chart(res['df'], symbol, current_tf)
                            caption = f"🚀 **GOLDEN CROSS** 🚀\nMA5 Cross UP MA10\n\n" + base_caption
                            if chart_file:
                                send_photo_sync(caption, chart_file)
                                if os.path.exists(chart_file): os.remove(chart_file)

                        elif 'MA30_TOUCH' in res['alerts']:
                            signals_found += 1
                            print(f"\n   [MA30] {symbol} MA30 Touch")
                            chart_file = generate_chart(res['df'], symbol, current_tf)
                            caption = f"⚠️ **MA 30 TOUCH** ⚠️\nCandle menyentuh garis MA30\n\n" + base_caption
                            if chart_file:
                                send_photo_sync(caption, chart_file)
                                if os.path.exists(chart_file): os.remove(chart_file)

                await asyncio.sleep(DELAY_BATCH)
            
            print(f"\n   ✅ Selesai {tf}. Sinyal ditemukan: {signals_found}")

    finally:
        await exchange.close()

# ================= LOOP UTAMA =================

async def main_scheduler():
    print("=== BOT CONTINUOUS SCANNER (Vol, RSI, MACD) STARTED ===")
    
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
        
        print(f"\n⏳ Menunggu {int(seconds_to_wait/60)} menit ({int(seconds_to_wait)} detik) sampai jam {scheduled_time.strftime('%H:%M:%S')}...")
        
        await asyncio.sleep(seconds_to_wait)
        
        print("\n⏰ WAKTU SCAN TIBA! Memulai analisa Multi-Timeframe...")
        await run_scanner_job()

if __name__ == "__main__":
    try:
        asyncio.run(main_scheduler())
    except KeyboardInterrupt:
        print("\nBot Stopped by User")



