import asyncio
import ccxt.async_support as ccxt
import pandas as pd
import mplfinance as mpf
import requests
import os
import time
import random
from datetime import datetime

# --- KONFIGURASI ENV ---
API_KEY = os.environ.get('BINANCE_API_KEY', 'fZwDMOfBL6rDU9jfUQox64fUAb2RSN48myxMPUGDAINYjmLdqJmUFhVRWLqlsX97')
API_SECRET = os.environ.get('BINANCE_API_SECRET', 'FmZNNbIOWIAddxVoLcNowLNW379E6gxyM85Bvy3QzlRMtK1eMApJp6vJtpGHWdWB')

# Telegram Config (Isi manual jika tidak pakai env var)
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', '8000712659:AAHltp77nGuakOzW9QMgQpVqnd5f1KgEsKA') 
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '-1003726025593')

# --- SETTING SCANNER ---
TIMEFRAMES = ['8h', '1d']  # Scan 8 Jam dan 1 Hari
TOP_N = 10                  # Ambil 3 Teratas (Gainer) dan 3 Terbawah (Loser)
MIN_PERCENTAGE = 2.5       # Filter minimal pergerakan 2% (agar tidak kirim koin sideway)

# --- SETTING KEAMANAN (ANTI-BAN) ---
BATCH_SIZE = 5      # Proses 5 koin per batch
BATCH_DELAY = 3     # Istirahat 3 detik antar batch (Sangat Aman)

# --- MEMORY UNTUK MENCEGAH LAPORAN GANDA ---
# Menyimpan timestamp laporan terakhir yang sukses dikirim
# Format: {'8h': timestamp, '1d': timestamp}
LAST_REPORTED_TIMESTAMP = {}

async def init_exchange():
    return ccxt.binance({
        'apiKey': API_KEY,
        'secret': API_SECRET,
        'enableRateLimit': True, 
        'options': {'defaultType': 'future'}
    })

# --- FUNGSI CHART & INDIKATOR ---
def calculate_indicators(df):
    # RSI
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).ewm(alpha=1/14, adjust=False).mean()
    loss = (-delta.where(delta < 0, 0)).ewm(alpha=1/14, adjust=False).mean()
    rs = gain / loss
    df['RSI'] = 100 - (100 / (1 + rs))
    
    # MA & BB
    df['MA5'] = df['close'].rolling(window=5).mean()
    df['MA20'] = df['close'].rolling(window=20).mean()
    df['MA100'] = df['close'].rolling(window=100).mean()
    df['BB_Upper'] = df['MA20'] + (2 * df['close'].rolling(window=20).std())
    df['BB_Lower'] = df['MA20'] - (2 * df['close'].rolling(window=20).std())
    return df

def generate_chart(symbol, timeframe, change_pct, rank_type, df):
    """
    Membuat file gambar chart
    rank_type: 'TOP GAINER' atau 'TOP LOSER'
    """
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return None

    clean_symbol = symbol.replace('/', '').replace(':', '')
    file_path = f"{rank_type}_{clean_symbol}_{timeframe}.png"
    
    try:
        plot_df = df.tail(50) # Ambil 50 candle terakhir
        
        # Style Custom
        mc = mpf.make_marketcolors(up='#2ebd85', down='#f6465d', edge='inherit', wick='inherit', volume='in')
        s  = mpf.make_mpf_style(marketcolors=mc, gridstyle='--', y_on_right=True)
        
        # Indikator
        apd = [
            mpf.make_addplot(plot_df['MA5'], color='blue', width=0.8, panel=0),
            mpf.make_addplot(plot_df['MA100'], color='black', width=1.2, panel=0),
            mpf.make_addplot(plot_df['BB_Upper'], color='gray', width=0.5, linestyle='--', panel=0),
            mpf.make_addplot(plot_df['BB_Lower'], color='gray', width=0.5, linestyle='--', panel=0),
            mpf.make_addplot(plot_df['RSI'], panel=2, color='#8e44ad', width=1.5, ylabel='RSI'),
            mpf.make_addplot([70]*len(plot_df), panel=2, color='red', width=0.5, linestyle='--'),
            mpf.make_addplot([30]*len(plot_df), panel=2, color='green', width=0.5, linestyle='--'),
        ]

        title = f"\n{rank_type} #{clean_symbol} [{timeframe}]\nChange: {change_pct:+.2f}%"
        
        mpf.plot(
            plot_df, type='candle', style=s, addplot=apd,
            title=title, ylabel='Price', volume=True, volume_panel=1, panel_ratios=(6, 2, 2), 
            savefig=file_path
        )
        return file_path
    except Exception as e:
        print(f"Gagal render chart {symbol}: {e}")
        return None

def send_telegram_ranking(ranking_data):
    """
    Mengirim laporan ranking ke Telegram.
    ranking_data: List of dict {'symbol', 'pct', 'df', 'rank_type', 'tf'}
    """
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    
    for item in ranking_data:
        symbol = item['symbol']
        tf = item['tf']
        pct = item['pct']
        rank_type = item['rank_type'] # 'GAINER 🟢' atau 'LOSER 🔴'
        df = item['df']
        
        # Buat Chart
        chart_path = generate_chart(symbol, tf, pct, item['rank_label'], df)
        
        if chart_path:
            # Caption Cantik
            caption = (
                f"🏆 **{item['rank_label']} REPORT** 🏆\n\n"
                f"{rank_type}\n"
                f"Coin: `#{symbol.replace('/','')}`\n"
                f"Timeframe: **{tf.upper()}**\n"
                f"Change: **{pct:+.2f}%**\n"
                f"Close Price: `{df['close'].iloc[-2]}`\n"
                f"RSI: {df['RSI'].iloc[-2]:.1f}\n"
            )
            
            with open(chart_path, 'rb') as photo:
                data = {'chat_id': TELEGRAM_CHAT_ID, 'caption': caption, 'parse_mode': 'Markdown'}
                requests.post(url, files={'photo': photo}, data=data)
            
            # Hapus file
            if os.path.exists(chart_path): os.remove(chart_path)
            
            time.sleep(1) # Delay pengiriman agar urutan rapi

async def fetch_ohlcv_safe(exchange, symbol, timeframe):
    try:
        # Ambil 150 candle
        ohlcv = await exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=150)
        return ohlcv
    except Exception:
        return None

async def process_batch_task(exchange, symbol, timeframe):
    """Mengambil data, menghitung persen, mengembalikan objek data"""
    ohlcv = await fetch_ohlcv_safe(exchange, symbol, timeframe)
    
    if not ohlcv or len(ohlcv) < 100: return None

    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    df.set_index('timestamp', inplace=True)

    # Ambil Candle CLOSED (Index -2)
    closed_candle = df.iloc[-2]
    open_p = float(closed_candle['open'])
    close_p = float(closed_candle['close'])
    candle_ts = closed_candle.name # Timestamp candle
    
    if open_p == 0: return None
    
    # Hitung Persentase Body
    change_pct = ((close_p - open_p) / open_p) * 100
    
    # Hitung indikator (untuk persiapan jika nanti terpilih jd juara)
    df = calculate_indicators(df)

    return {
        'symbol': symbol,
        'tf': timeframe,
        'pct': change_pct,
        'ts': candle_ts,
        'df': df
    }

async def run_ranking_bot():
    print("🤖 Bot Rank & Report Aktif (Top Gainers/Losers)")
    exchange = await init_exchange()
    
    try:
        # 1. Load Market Sekali
        print("📥 Loading markets...")
        markets = await exchange.load_markets()
        symbols = [s for s in markets if '/USDT' in s]
        print(f"📊 Total Market: {len(symbols)} pairs")
        
        while True:
            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 🏁 Memulai Scan Keseluruhan...")
            
            # Container untuk menampung SEMUA hasil scan sementara
            # Format: {'8h': [list_data], '1d': [list_data]}
            all_results = {tf: [] for tf in TIMEFRAMES}
            
            # --- FASE 1: COLLECT DATA (BATCHING) ---
            tasks_queue = []
            for sym in symbols:
                for tf in TIMEFRAMES:
                    tasks_queue.append((sym, tf))
            
            total_tasks = len(tasks_queue)
            
            for i in range(0, total_tasks, BATCH_SIZE):
                batch = tasks_queue[i : i + BATCH_SIZE]
                coroutines = [process_batch_task(exchange, s, t) for s, t in batch]
                
                # Jalankan parallel
                results = await asyncio.gather(*coroutines)
                
                # Masukkan hasil valid ke container
                for res in results:
                    if res:
                        all_results[res['tf']].append(res)
                
                print(f"⏳ Collecting Data... {i}/{total_tasks} ({len(all_results['8h'])+len(all_results['1d'])} ok)", end='\r')
                await asyncio.sleep(BATCH_DELAY) # Sleep Anti-Ban

            print("\n✅ Data Collection Selesai. Menganalisa Ranking...")

            # --- FASE 2: SORTING & RANKING ---
            for tf in TIMEFRAMES:
                data_tf = all_results[tf]
                if not data_tf: continue
                
                # Cek Timestamp Global untuk Timeframe ini
                # Kita ambil sample timestamp dari BTC (atau data pertama yg valid)
                # Tujuannya: Cek apakah ini candle BARU yang belum pernah dilaporkan?
                sample_ts = data_tf[0]['ts']
                
                if tf in LAST_REPORTED_TIMESTAMP:
                    if LAST_REPORTED_TIMESTAMP[tf] == sample_ts:
                        print(f"⏩ Laporan {tf} untuk {sample_ts} SUDAH dikirim sebelumnya. Skip.")
                        continue # Skip timeframe ini, lanjut ke timeframe berikutnya
                
                print(f"📊 Menyusun Ranking untuk Timeframe {tf.upper()}...")
                
                # 1. Sort by Percent (Tertinggi ke Terendah)
                sorted_data = sorted(data_tf, key=lambda x: x['pct'], reverse=True)
                
                final_report_list = []
                
                # 2. Ambil TOP GAINERS (Positif Tertinggi)
                gainers = [x for x in sorted_data if x['pct'] > MIN_PERCENTAGE]
                top_gainers = gainers[:TOP_N] # Ambil 3 teratas
                
                for g in top_gainers:
                    g['rank_type'] = '🚀 TOP GAINER'
                    g['rank_label'] = 'TOP GAINER'
                    final_report_list.append(g)
                    
                # 3. Ambil TOP LOSERS (Negatif Tertinggi / Paling Bawah)
                # Sort ulang ascending untuk ambil yg minusnya paling besar
                sorted_losers = sorted(data_tf, key=lambda x: x['pct']) 
                losers = [x for x in sorted_losers if x['pct'] < -MIN_PERCENTAGE]
                top_losers = losers[:TOP_N] # Ambil 3 terbawah
                
                for l in top_losers:
                    l['rank_type'] = '🔻 TOP LOSER'
                    l['rank_label'] = 'TOP LOSER'
                    final_report_list.append(l)

                # --- FASE 3: SEND REPORT ---
                if final_report_list:
                    print(f"📤 Mengirim {len(final_report_list)} chart ke Telegram...")
                    send_telegram_ranking(final_report_list)
                    
                    # Tandai timestamp ini sudah dilaporkan
                    LAST_REPORTED_TIMESTAMP[tf] = sample_ts
                    print(f"✅ Laporan {tf} selesai. Timestamp tercatat: {sample_ts}")
                else:
                    print(f"⚠️ Tidak ada koin yang tembus +/- {MIN_PERCENTAGE}% di {tf}")

            # Jeda Panjang setelah satu putaran penuh
            print("\n💤 Siklus selesai. Tidur 5 menit sebelum cek candle baru...")
            await asyncio.sleep(300)

    except Exception as e:
        print(f"Critical Error: {e}")
    finally:
        await exchange.close()

if __name__ == "__main__":
    try:
        asyncio.run(run_ranking_bot())
    except KeyboardInterrupt:
        print("Bot Stopped.")


