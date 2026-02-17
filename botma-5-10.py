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
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '-1003835878828')

# Setting Scanner
TIMEFRAME = '1h'       # Timeframe 1 Jam
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

def generate_chart(df, symbol):
    """Membuat chart png secara lokal"""
    filename = f"chart_{symbol.replace('/', '')}_{int(time.time())}.png"
    try:
        # Ambil 50 candle terakhir agar chart jelas
        plot_df = df.tail(50).copy()
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
        
        mpf.plot(plot_df, type='candle', style=s, addplot=ap, 
                 title=f"{symbol} ({TIMEFRAME}) - MA30 Alert",
                 savefig=dict(fname=filename, dpi=80, bbox_inches='tight'), volume=False)
        return filename
    except Exception as e:
        print(f"Error making chart {symbol}: {e}")
        return None

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

async def process_coin(exchange, symbol):
    """Proses 1 koin: Fetch -> Calculate -> Analyze"""
    try:
        # 1. Fetch Candle (Async)
        bars = await exchange.fetch_ohlcv(symbol, TIMEFRAME, limit=LIMIT_CANDLES)
        if not bars or len(bars) < 35: return None

        # 2. DataFrame Processing
        df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')

        # 3. Indikator
        df['MA5'] = df['close'].rolling(window=5).mean()
        df['MA10'] = df['close'].rolling(window=10).mean()
        df['MA30'] = df['close'].rolling(window=30).mean()

        # 4. Ambil Candle Close Terakhir (Index -2)
        # Index -1 adalah candle berjalan (belum close)
        last_closed = df.iloc[-2]
        prev_closed = df.iloc[-3]
        
        result = {'symbol': symbol, 'alerts': [], 'data': last_closed, 'df': df}
        has_alert = False

        # --- LOGIKA 1: GOLDEN CROSS MA5 & MA10 ---
        # (MA5 Kemarin <= MA10 Kemarin) DAN (MA5 Sekarang > MA10 Sekarang)
        if prev_closed['MA5'] <= prev_closed['MA10'] and last_closed['MA5'] > last_closed['MA10']:
            result['alerts'].append('GOLDEN_CROSS')
            has_alert = True

        # --- LOGIKA 2: MA30 TOUCH ---
        # Low <= MA30 <= High
        if last_closed['low'] <= last_closed['MA30'] <= last_closed['high']:
            result['alerts'].append('MA30_TOUCH')
            has_alert = True

        return result if has_alert else None

    except Exception as e:
        return None

async def run_scanner_job():
    """Menjalankan 1 putaran scan penuh"""
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 🔄 Memulai Scan {TOP_COINS} Koin...")
    
    exchange = ccxt.binance({
        'apiKey': BINANCE_API_KEY,
        'secret': BINANCE_SECRET_KEY,
        'enableRateLimit': False, # Kita handle manual via batching
        'options': {'defaultType': 'future'}
    })

    try:
        symbols = await get_top_futures_symbols(exchange)
        if not symbols:
            print("Gagal mengambil simbol.")
            await exchange.close()
            return

        tasks = []
        signals_found = 0
        
        # Loop per Batch (Agar tidak kena rate limit)
        for i in range(0, len(symbols), BATCH_SIZE):
            batch = symbols[i : i + BATCH_SIZE]
            print(f"Scanning batch {i+1}-{min(i+BATCH_SIZE, len(symbols))}...", end='\r')
            
            # Buat list pekerjaan (tasks)
            coroutines = [process_coin(exchange, sym) for sym in batch]
            results = await asyncio.gather(*coroutines)

            # Proses Hasil Batch Ini
            for res in results:
                if res:
                    symbol = res['symbol']
                    price = res['data']['close']
                    ma30_val = res['data']['MA30']
                    
                    # Kirim Alert Golden Cross
                    if 'GOLDEN_CROSS' in res['alerts']:
                        signals_found += 1
                        print(f"\n[SIGNAL] Golden Cross: {symbol}")
                        msg = (
                            f"🚀 **GOLDEN CROSS CONFIRMED** 🚀\n\n"
                            f"#{symbol.replace('/','')}\n"
                            f"Harga Close: {price}\n"
                            f"Timeframe: {TIMEFRAME}\n"
                            f"MA5 Cross UP MA10"
                        )
                        send_telegram_sync(msg)

                    # Kirim Alert MA30 Touch
                    if 'MA30_TOUCH' in res['alerts']:
                        signals_found += 1
                        print(f"\n[SIGNAL] MA30 Touch: {symbol}")
                        chart_file = generate_chart(res['df'], symbol)
                        caption = (
                            f"⚠️ **MA 30 TOUCH CONFIRMED** ⚠️\n\n"
                            f"#{symbol.replace('/','')}\n"
                            f"Harga Close: {price}\n"
                            f"MA30: {ma30_val:.4f}\n"
                            f"Candle menyentuh garis MA30"
                        )
                        if chart_file:
                            send_photo_sync(caption, chart_file)
                            if os.path.exists(chart_file): os.remove(chart_file)

            # Jeda antar batch (Penting!)
            await asyncio.sleep(DELAY_BATCH)
            
        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] ✅ Scan Selesai. Total Signal: {signals_found}")

    finally:
        await exchange.close()

# ================= LOOP UTAMA =================

async def main_scheduler():
    print("=== BOT CONTINUOUS SCANNER STARTED ===")
    
    # Jalankan scan pertama kali saat bot baru dinyalakan (opsional, agar tau bot jalan)
    await run_scanner_job()
    
    while True:
        now = datetime.now()
        
        # Hitung waktu menuju jam berikutnya (Next Hour : Minute 00 : Second 00)
        # Contoh: Jika sekarang 13:15, next run adalah 14:00
        next_run = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        
        # Tambahkan buffer 10 detik agar candle benar-benar close di server Binance
        # Jadi kita mulai scan jam 14:00:10
        scheduled_time = next_run + timedelta(seconds=10)
        
        seconds_to_wait = (scheduled_time - now).total_seconds()
        
        print(f"\n⏳ Menunggu {int(seconds_to_wait/60)} menit ({int(seconds_to_wait)} detik) sampai jam {scheduled_time.strftime('%H:%M:%S')}...")
        print("Bot dalam mode standby...")
        
        # Tidur sampai waktu yang ditentukan
        await asyncio.sleep(seconds_to_wait)
        
        # WAKTUNYA SCAN!
        print("\n⏰ WAKTU SCAN TIBA! Memulai analisa...")
        await run_scanner_job()

if __name__ == "__main__":
    try:
        asyncio.run(main_scheduler())
    except KeyboardInterrupt:
        print("\nBot Stopped by User")


