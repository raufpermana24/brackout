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
# Filter Persentase Minimum (Hanya kirim jika >= 2.5 atau <= -2.5)
MIN_CHANGE_THRESHOLD = 2.5

# Jumlah Maksimal Top Koin yang dikirim (untuk mencegah spam jika market sedang volatile sekali)
# Bot akan mengambil koin yang lolos filter, lalu mengirim maksimal 100 teratas dari list tersebut.
TOP_COUNT = 100 

# Jeda antar pengiriman gambar (5 detik)
DELAY_SEND = 5
# Timeframe untuk Chart
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
    if df is None or len(df) < 50: return None

    clean_symbol = symbol.replace('/', '')
    file_path = f"rank_{rank_num}_{clean_symbol}.png"
    
    try:
        # Indikator
        df['MA5'] = df['close'].rolling(5).mean()
        df['MA20'] = df['close'].rolling(20).mean()
        df['MA100'] = df['close'].rolling(100).mean()
        
        # BB
        std = df['close'].rolling(20).std()
        df['BB_Upper'] = df['MA20'] + (2 * std)
        df['BB_Lower'] = df['MA20'] - (2 * std)
        
        # RSI
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).ewm(alpha=1/14, adjust=False).mean()
        loss = (-delta.where(delta < 0, 0)).ewm(alpha=1/14, adjust=False).mean()
        df['RSI'] = 100 - (100 / (1 + (gain / loss)))

        plot_df = df.tail(60)
        
        # Fix Volume Ylim Singular Warning
        vol_min = plot_df['volume'].min()
        vol_max = plot_df['volume'].max()
        show_volume = True
        if vol_min == vol_max: show_volume = False
        
        # Konfigurasi Panel Dinamis
        if show_volume:
            rsi_panel = 2
            p_ratios = (6, 2, 2)
            vol_panel = 1
        else:
            rsi_panel = 1
            p_ratios = (6, 2)
            # FIX: Validator mplfinance menolak 'None'. 
            # Kita set ke integer 1. Saat volume=False, ini tidak akan digambar, 
            # tapi validator akan puas karena nilainya integer.
            vol_panel = 1 
        
        apd = [
            mpf.make_addplot(plot_df['MA5'], color='blue', width=0.8, panel=0),
            mpf.make_addplot(plot_df['MA100'], color='black', width=1.2, panel=0),
            mpf.make_addplot(plot_df['BB_Upper'], color='gray', linestyle='--', width=0.5, panel=0),
            mpf.make_addplot(plot_df['BB_Lower'], color='gray', linestyle='--', width=0.5, panel=0),
            mpf.make_addplot(plot_df['RSI'], panel=rsi_panel, color='purple', ylabel='RSI', width=1.5),
            mpf.make_addplot([70]*len(plot_df), panel=rsi_panel, color='red', linestyle='--', width=0.5),
            mpf.make_addplot([30]*len(plot_df), panel=rsi_panel, color='green', linestyle='--', width=0.5),
        ]
        
        mc = mpf.make_marketcolors(up='#2ebd85', down='#f6465d', edge='inherit', wick='inherit', volume='in')
        s  = mpf.make_mpf_style(marketcolors=mc, gridstyle='--', y_on_right=True)
        
        title = f"\n#{rank_num} {rank_type}: {symbol}\nChange 24H: {change_pct:+.2f}%"
        
        mpf.plot(
            plot_df, type='candle', style=s, addplot=apd,
            title=title, ylabel='Price', 
            volume=show_volume, volume_panel=vol_panel, 
            panel_ratios=p_ratios, 
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

async def fetch_ohlcv_safe(exchange, symbol, timeframe):
    max_retries = 3
    for i in range(max_retries):
        try:
            return await exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=150)
        except (ccxt.RateLimitExceeded, ccxt.DDoSProtection):
            wait_time = 60 * (i + 1)
            print(f"\n⛔ Rate Limit Hit pada {symbol}. Tidur {wait_time} detik...")
            await asyncio.sleep(wait_time)
        except Exception as e:
            err_str = str(e).lower()
            if '429' in err_str or 'too many requests' in err_str:
                wait_time = 60 * (i + 2)
                print(f"\n⛔ Binance 429 Error pada {symbol}. Tidur {wait_time} detik...")
                await asyncio.sleep(wait_time)
            elif '418' in err_str or 'banned' in err_str:
                print(f"\n💀 IP BANNED. Bot tidur 1 Jam.")
                await asyncio.sleep(3600)
                return None
            else:
                await asyncio.sleep(5)
    return None

async def process_and_send_list(exchange, sorted_list, rank_type_label):
    total = len(sorted_list)
    if total == 0:
        print(f"⚠️ Tidak ada koin yang memenuhi syarat > {MIN_CHANGE_THRESHOLD}% untuk {rank_type_label}")
        return

    print(f"🚀 Memulai pengiriman {total} chart untuk kategori: {rank_type_label}")
    
    for i, item in enumerate(sorted_list, 1):
        symbol = item['symbol']
        pct = item['pct']
        price = item['price']
        
        print(f"[{i}/{total}] Mengambil data history {symbol}...")
        
        try:
            ohlcv = await fetch_ohlcv_safe(exchange, symbol, CHART_TIMEFRAME)
            
            if ohlcv:
                df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
                df.set_index('timestamp', inplace=True)
                
                chart_path = generate_chart_image(symbol, pct, df, i, rank_type_label)
                
                if chart_path:
                    icon = "🔥" if rank_type_label == "TOP GAINER" else "🩸"
                    rsi_val = df['RSI'].iloc[-1] if 'RSI' in df else 0
                    
                    caption = (
                        f"{icon} **#{i} {rank_type_label}**\n"
                        f"Coin: `{symbol}`\n"
                        f"Change 24H: **{pct:+.2f}%**\n"
                        f"Price: `{price}`\n"
                        f"RSI: {rsi_val:.1f}"
                    )
                    
                    print(f"📤 Mengirim chart {symbol} ({pct:.2f}%) ke Telegram...")
                    await send_telegram_photo(chart_path, caption)
                    
                    if os.path.exists(chart_path): os.remove(chart_path)
                    
                    print(f"⏳ Jeda {DELAY_SEND} detik...")
                    await asyncio.sleep(DELAY_SEND)
            else:
                print(f"⚠️ Data history kosong untuk {symbol} (Skip)")

        except Exception as e:
            print(f"Error processing {symbol}: {e}")
            await asyncio.sleep(1) 

async def run_chart_ranker():
    exchange = await get_exchange()
    print(f"🤖 Bot Chart Ranker Filtered Berjalan...")
    print(f"🛡️ Syarat Lolos: Persentase >= {MIN_CHANGE_THRESHOLD}% atau <= -{MIN_CHANGE_THRESHOLD}%")
    print(f"🎯 Target Maksimal: Top {TOP_COUNT} Gainers & {TOP_COUNT} Losers dari hasil filter")
    
    try:
        while True:
            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 📥 Mengambil Snapshot Pasar...")
            
            tickers = await exchange.fetch_tickers()
            
            market_data = []
            for symbol, ticker in tickers.items():
                if '/USDT' in symbol: 
                    pct = ticker['percentage'] if ticker['percentage'] else 0.0
                    
                    # --- LOGIKA FILTER KETAT ---
                    # Jika pct ada di range "tanggung" (misal -1.5 atau 0.5 atau 2.0) -> SKIP
                    if abs(pct) < MIN_CHANGE_THRESHOLD:
                        continue 
                    
                    # Jika lolos (>= 2.5 atau <= -2.5), simpan!
                    market_data.append({
                        'symbol': symbol,
                        'pct': pct,
                        'price': ticker['last']
                    })
            
            print(f"📊 Total Koin Lolos Filter (+/- {MIN_CHANGE_THRESHOLD}%): {len(market_data)} koin.")
            
            # Pisahkan Gainer dan Loser dari data yang sudah difilter
            gainers_only = [x for x in market_data if x['pct'] > 0]
            losers_only = [x for x in market_data if x['pct'] < 0]

            # 2. SORTING
            # Sort Gainer (Besar ke Kecil)
            sorted_gainers = sorted(gainers_only, key=lambda x: x['pct'], reverse=True)
            # Ambil Top N (misal 100) dari yg lolos filter
            final_gainers = sorted_gainers[:TOP_COUNT]
            
            # Sort Loser (Kecil ke Besar / Minus paling dalam)
            sorted_losers = sorted(losers_only, key=lambda x: x['pct'])
            # Ambil Top N (misal 100) dari yg lolos filter
            final_losers = sorted_losers[:TOP_COUNT]
            
            # 3. PROSES PENGIRIMAN
            await process_and_send_list(exchange, final_gainers, "TOP GAINER")
            
            print("\n✅ Gainers Selesai. Istirahat 10 detik...\n")
            await asyncio.sleep(10)
            
            await process_and_send_list(exchange, final_losers, "TOP LOSER")
            
            print("\n🏁 Siklus Selesai. Bot tidur 1 Jam.")
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
