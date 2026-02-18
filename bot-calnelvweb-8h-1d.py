import asyncio
import ccxt.async_support as ccxt
import pandas as pd
import mplfinance as mpf
import requests
import os
import time
from datetime import datetime

# --- KONFIGURASI ENV ---
API_KEY = os.environ.get('BINANCE_API_KEY', 'fZwDMOfBL6rDU9jfUQox64fUAb2RSN48myxMPUGDAINYjmLdqJmUFhVRWLqlsX97')
API_SECRET = os.environ.get('BINANCE_API_SECRET', 'FmZNNbIOWIAddxVoLcNowLNW379E6gxyM85Bvy3QzlRMtK1eMApJp6vJtpGHWdWB')

# Telegram Config (Isi manual jika tidak pakai env var)
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', '8000712659:AAHltp77nGuakOzW9QMgQpVqnd5f1KgEsKA') 
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '-1003726025593')


# --- SETTING ---
# Jumlah Top Koin (Sesuai request: 100 Gainer & 100 Loser)
TOP_COUNT = 100 
# Jeda antar pengiriman gambar (Sesuai request: 5 detik)
DELAY_SEND = 5
# Timeframe untuk Chart yang akan digambar
CHART_TIMEFRAME = '1d' 

async def get_exchange():
    return ccxt.binance({
        'apiKey': API_KEY,
        'secret': API_SECRET,
        'enableRateLimit': True,
        'options': {'defaultType': 'future'}
    })

# --- FUNGSI CHART & INDIKATOR ---
def generate_chart_image(symbol, change_pct, df, rank_num, rank_type):
    """
    Membuat file gambar chart candlestick dengan indikator
    """
    if df is None or len(df) < 50: return None

    # Nama file unik
    clean_symbol = symbol.replace('/', '')
    file_path = f"rank_{rank_num}_{clean_symbol}.png"
    
    try:
        # Hitung Indikator
        df['MA5'] = df['close'].rolling(5).mean()
        df['MA20'] = df['close'].rolling(20).mean() # Basis BB
        df['MA100'] = df['close'].rolling(100).mean()
        
        # Bollinger Bands
        std = df['close'].rolling(20).std()
        df['BB_Upper'] = df['MA20'] + (2 * std)
        df['BB_Lower'] = df['MA20'] - (2 * std)
        
        # RSI
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).ewm(alpha=1/14, adjust=False).mean()
        loss = (-delta.where(delta < 0, 0)).ewm(alpha=1/14, adjust=False).mean()
        df['RSI'] = 100 - (100 / (1 + (gain / loss)))

        # Ambil 60 candle terakhir untuk visualisasi
        plot_df = df.tail(60)
        
        # Setup Plot
        apd = [
            mpf.make_addplot(plot_df['MA5'], color='blue', width=0.8, panel=0),
            mpf.make_addplot(plot_df['MA100'], color='black', width=1.2, panel=0),
            mpf.make_addplot(plot_df['BB_Upper'], color='gray', linestyle='--', width=0.5, panel=0),
            mpf.make_addplot(plot_df['BB_Lower'], color='gray', linestyle='--', width=0.5, panel=0),
            mpf.make_addplot(plot_df['RSI'], panel=2, color='purple', ylabel='RSI', width=1.5),
            mpf.make_addplot([70]*len(plot_df), panel=2, color='red', linestyle='--', width=0.5),
            mpf.make_addplot([30]*len(plot_df), panel=2, color='green', linestyle='--', width=0.5),
        ]
        
        mc = mpf.make_marketcolors(up='#2ebd85', down='#f6465d', edge='inherit', wick='inherit', volume='in')
        s  = mpf.make_mpf_style(marketcolors=mc, gridstyle='--', y_on_right=True)
        
        # Judul Chart
        title = f"\n#{rank_num} {rank_type}: {symbol}\nChange 24H: {change_pct:+.2f}%"
        
        mpf.plot(
            plot_df, type='candle', style=s, addplot=apd,
            title=title, ylabel='Price', volume=True, volume_panel=1, panel_ratios=(6, 2, 2), 
            savefig=file_path
        )
        return file_path
    
    except Exception as e:
        print(f"❌ Gagal generate chart {symbol}: {e}")
        return None

async def send_telegram_photo(file_path, caption):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    try:
        with open(file_path, 'rb') as photo:
            data = {'chat_id': TELEGRAM_CHAT_ID, 'caption': caption, 'parse_mode': 'Markdown'}
            requests.post(url, files={'photo': photo}, data=data)
    except Exception as e:
        print(f"❌ Error Telegram: {e}")

async def process_and_send_list(exchange, sorted_list, rank_type_label):
    """
    Melakukan loop untuk mengambil history chart dan mengirimnya satu per satu
    """
    total = len(sorted_list)
    print(f"🚀 Memulai pengiriman {total} chart untuk kategori: {rank_type_label}")
    
    for i, item in enumerate(sorted_list, 1):
        symbol = item['symbol']
        pct = item['pct']
        price = item['price']
        
        print(f"[{i}/{total}] Mengambil data history {symbol}...")
        
        try:
            # Kita perlu fetch OHLCV khusus untuk koin ini agar bisa digambar
            ohlcv = await exchange.fetch_ohlcv(symbol, timeframe=CHART_TIMEFRAME, limit=150)
            
            if ohlcv:
                # Convert ke DataFrame
                df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
                df.set_index('timestamp', inplace=True)
                
                # 1. Buat Gambar Chart
                chart_path = generate_chart_image(symbol, pct, df, i, rank_type_label)
                
                if chart_path:
                    # 2. Siapkan Caption
                    icon = "🔥" if rank_type_label == "TOP GAINER" else "🩸"
                    rsi_val = df['RSI'].iloc[-1] if 'RSI' in df else 0
                    
                    caption = (
                        f"{icon} **#{i} {rank_type_label}**\n"
                        f"Coin: `{symbol}`\n"
                        f"Change 24H: **{pct:+.2f}%**\n"
                        f"Price: `{price}`\n"
                        f"RSI: {rsi_val:.1f}"
                    )
                    
                    # 3. Kirim ke Telegram
                    print(f"📤 Mengirim chart {symbol} ke Telegram...")
                    await send_telegram_photo(chart_path, caption)
                    
                    # Hapus file
                    if os.path.exists(chart_path): os.remove(chart_path)
                    
                    # 4. JEDA 5 DETIK (Sesuai Request)
                    print(f"⏳ Jeda {DELAY_SEND} detik...")
                    await asyncio.sleep(DELAY_SEND)
            
            else:
                print(f"⚠️ Tidak ada data history untuk {symbol}")

        except Exception as e:
            print(f"Error processing {symbol}: {e}")
            await asyncio.sleep(1) # Sleep sebentar jika error biar gak crash loop

async def run_chart_ranker():
    exchange = await get_exchange()
    print(f"🤖 Bot Chart Ranker Berjalan...")
    print(f"🎯 Target: Top {TOP_COUNT} Gainers & {TOP_COUNT} Losers")
    print(f"⏱️ Jeda Kirim: {DELAY_SEND} detik per gambar")
    
    try:
        while True:
            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 📥 Mengambil Snapshot Pasar...")
            
            # 1. AMBIL SEMUA DATA (Snapshot)
            tickers = await exchange.fetch_tickers()
            
            market_data = []
            for symbol, ticker in tickers.items():
                if '/USDT' in symbol: # Hanya pair USDT
                    market_data.append({
                        'symbol': symbol,
                        'pct': ticker['percentage'] if ticker['percentage'] else 0.0,
                        'price': ticker['last']
                    })
            
            print(f"📊 Total Data: {len(market_data)} koin.")
            
            # 2. SORTING DI MEMORY
            # Sort Descending (Besar ke Kecil) untuk Gainers
            sorted_desc = sorted(market_data, key=lambda x: x['pct'], reverse=True)
            top_gainers = sorted_desc[:TOP_COUNT]
            
            # Sort Ascending (Kecil ke Besar) untuk Losers
            sorted_asc = sorted(market_data, key=lambda x: x['pct'])
            top_losers = sorted_asc[:TOP_COUNT]
            
            # 3. PROSES PENGIRIMAN (Gainers Dulu)
            await process_and_send_list(exchange, top_gainers, "TOP GAINER")
            
            print("\n✅ Top Gainers Selesai. Istirahat 10 detik sebelum Losers...\n")
            await asyncio.sleep(10)
            
            # 4. PROSES PENGIRIMAN (Losers Kemudian)
            await process_and_send_list(exchange, top_losers, "TOP LOSER")
            
            print("\n🏁 Siklus Selesai. Bot akan tidur 1 Jam sebelum scan ulang.")
            # Tidur panjang sebelum mengulang proses dari awal
            await asyncio.sleep(3600)

    except Exception as e:
        print(f"Critical Error: {e}")
    finally:
        await exchange.close()

if __name__ == "__main__":
    try:
        asyncio.run(run_chart_ranker())
    except KeyboardInterrupt:
        print("Bot Stopped.")


