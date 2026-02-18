import asyncio
import json
import websockets
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

# --- KONFIGURASI SCANNER ---
# Kita subscribe ke candle 8 Jam dan 1 Hari
TIMEFRAMES_MAP = {'8h': '8h', '1d': '1d'} 
TOP_N = 3              # Top 3 Gainer & Loser
MIN_PERCENTAGE = 2.0   # Minimal pergerakan 2%

# URL WebSocket Binance Futures
WS_URL = "wss://fstream.binance.com/ws"

# Buffer untuk menampung candle yang BARU SAJA close
# Format: {'8h': [list_data], '1d': [list_data]}
CLOSED_CANDLES_BUFFER = {'8h': [], '1d': []}

# Lock untuk mencegah race condition saat memproses buffer
BUFFER_LOCK = asyncio.Lock()

async def get_exchange():
    return ccxt.binance({
        'apiKey': API_KEY,
        'secret': API_SECRET,
        'enableRateLimit': True,
        'options': {'defaultType': 'future'}
    })

# --- FUNGSI CHART & INDIKATOR (Dipakai saat Reporting) ---
def calculate_indicators(df):
    df['MA5'] = df['close'].rolling(5).mean()
    df['MA100'] = df['close'].rolling(100).mean()
    
    # BB
    df['MA20'] = df['close'].rolling(20).mean()
    std = df['close'].rolling(20).std()
    df['BB_Upper'] = df['MA20'] + (2 * std)
    df['BB_Lower'] = df['MA20'] - (2 * std)
    
    # RSI
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).ewm(alpha=1/14, adjust=False).mean()
    loss = (-delta.where(delta < 0, 0)).ewm(alpha=1/14, adjust=False).mean()
    df['RSI'] = 100 - (100 / (1 + (gain / loss)))
    return df

async def fetch_history_and_chart(symbol, timeframe, change_pct, rank_type):
    """
    Mengambil data history (via REST API) HANYA untuk koin terpilih
    untuk keperluan menggambar Chart MA/BB.
    """
    exchange = await get_exchange()
    file_path = f"{rank_type}_{symbol.replace('/','')}_{timeframe}.png"
    
    try:
        # Kita butuh ~150 candle untuk MA100
        ohlcv = await exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=150)
        await exchange.close()
        
        if not ohlcv: return None
        
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.set_index('timestamp', inplace=True)
        
        df = calculate_indicators(df)
        plot_df = df.tail(60)
        
        # Visualisasi
        apd = [
            mpf.make_addplot(plot_df['MA5'], color='blue', panel=0, width=0.8),
            mpf.make_addplot(plot_df['MA100'], color='black', panel=0, width=1.2),
            mpf.make_addplot(plot_df['BB_Upper'], color='gray', linestyle='--', panel=0),
            mpf.make_addplot(plot_df['BB_Lower'], color='gray', linestyle='--', panel=0),
            mpf.make_addplot(plot_df['RSI'], panel=2, color='purple', ylabel='RSI'),
            mpf.make_addplot([70]*len(plot_df), panel=2, color='red', linestyle='--'),
            mpf.make_addplot([30]*len(plot_df), panel=2, color='green', linestyle='--'),
        ]
        
        mc = mpf.make_marketcolors(up='#2ebd85', down='#f6465d', edge='inherit', wick='inherit', volume='in')
        s  = mpf.make_mpf_style(marketcolors=mc, gridstyle='--', y_on_right=True)
        
        title = f"\n{rank_type} #{symbol.replace('/','')} [{timeframe}]\nChange: {change_pct:+.2f}%"
        
        mpf.plot(
            plot_df, type='candle', style=s, addplot=apd,
            title=title, ylabel='Price', volume=True, volume_panel=1, panel_ratios=(6,2,2),
            savefig=file_path
        )
        return file_path, df['close'].iloc[-1], df['RSI'].iloc[-1]

    except Exception as e:
        print(f"Chart Error ({symbol}): {e}")
        await exchange.close()
        return None, 0, 0

async def send_telegram_report(report_list):
    """Kirim Top 3 Gainer/Loser ke Telegram"""
    if not TELEGRAM_TOKEN: return
    
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    
    for item in report_list:
        symbol = item['symbol']
        tf = item['tf']
        pct = item['pct']
        rank_type = item['rank_type']
        
        print(f"🎨 Generating Chart untuk {symbol} ({rank_type})...")
        chart_path, close_price, rsi_val = await fetch_history_and_chart(symbol, tf, pct, rank_type)
        
        if chart_path:
            caption = (
                f"🏆 **{rank_type} REPORT** 🏆\n\n"
                f"Coin: `#{symbol.replace('/','')}`\n"
                f"Timeframe: **{tf.upper()}**\n"
                f"Change: **{pct:+.2f}%**\n"
                f"Close Price: `{close_price}`\n"
                f"RSI: {rsi_val:.1f}\n"
            )
            
            try:
                with open(chart_path, 'rb') as photo:
                    data = {'chat_id': TELEGRAM_CHAT_ID, 'caption': caption, 'parse_mode': 'Markdown'}
                    requests.post(url, files={'photo': photo}, data=data)
            except Exception as e:
                print(f"Telegram Send Error: {e}")
            
            if os.path.exists(chart_path): os.remove(chart_path)
            time.sleep(1) # Delay sopan

async def process_buffer_and_report():
    """
    Looping terpisah untuk mengecek apakah buffer sudah penuh/waktunya lapor.
    Ini berjalan parallel dengan WebSocket listener.
    """
    while True:
        await asyncio.sleep(5) # Cek buffer setiap 5 detik
        
        async with BUFFER_LOCK:
            for tf in ['8h', '1d']:
                buffer_data = CLOSED_CANDLES_BUFFER[tf]
                
                # Logika: Jika ada banyak data masuk (berarti candle baru saja close massal)
                # Kita tunggu sebentar agar semua data pair masuk, baru kita ranking.
                if len(buffer_data) > 0:
                    print(f"📥 Buffer {tf}: {len(buffer_data)} candle closed. Menunggu data lengkap...")
                    
                    # Tunggu 10 detik lagi untuk memastikan semua pair (BTC, ETH, Altcoins) masuk
                    await asyncio.sleep(10)
                    
                    # --- MULAI RANKING ---
                    print(f"📊 Menyusun Ranking {tf.upper()} dari {len(buffer_data)} koin...")
                    
                    # 1. Sort Gainer (Tertinggi)
                    sorted_desc = sorted(buffer_data, key=lambda x: x['pct'], reverse=True)
                    top_gainers = [x for x in sorted_desc if x['pct'] >= MIN_PERCENTAGE][:TOP_N]
                    
                    # 2. Sort Loser (Terendah / Minus Besar)
                    sorted_asc = sorted(buffer_data, key=lambda x: x['pct'])
                    top_losers = [x for x in sorted_asc if x['pct'] <= -MIN_PERCENTAGE][:TOP_N]
                    
                    final_report = []
                    for g in top_gainers:
                        g['rank_type'] = 'TOP GAINER 🚀'
                        final_report.append(g)
                    for l in top_losers:
                        l['rank_type'] = 'TOP LOSER 🔻'
                        final_report.append(l)
                    
                    if final_report:
                        print(f"📤 Mengirim {len(final_report)} laporan ke Telegram...")
                        await send_telegram_report(final_report)
                    else:
                        print(f"⚠️ Tidak ada koin yang tembus +/- {MIN_PERCENTAGE}% di {tf}")
                    
                    # --- BERSIHKAN BUFFER ---
                    # Hapus data agar tidak diproses ulang
                    CLOSED_CANDLES_BUFFER[tf] = []

async def websocket_listener():
    """Fungsi Utama WebSocket"""
    exchange = await get_exchange()
    
    # 1. Ambil List Symbol dulu
    print("📥 Fetching Markets (REST)...")
    markets = await exchange.load_markets()
    symbols = [s for s in markets if s.endswith('/USDT')]
    # Simbol untuk stream harus lowercase dan tanpa '/' (contoh: btcusdt)
    stream_symbols = [s.replace('/', '').lower() for s in symbols]
    await exchange.close()
    
    print(f"✅ Loaded {len(stream_symbols)} pairs.")

    # 2. Bangun List Subscription
    # Kita butuh kline_8h dan kline_1d untuk SEMUA symbol
    params = []
    for s in stream_symbols:
        params.append(f"{s}@kline_8h")
        params.append(f"{s}@kline_1d")
    
    # Binance membatasi params per request, jadi kita pecah (batch subscription)
    # Maksimal sekitar 200 streams per request subscription
    BATCH_SUB_SIZE = 200
    
    async for websocket in websockets.connect(WS_URL):
        try:
            print("🔌 Connected to WebSocket. Subscribing streams...")
            
            # Kirim request SUBSCRIBE bertahap
            for i in range(0, len(params), BATCH_SUB_SIZE):
                batch = params[i:i+BATCH_SUB_SIZE]
                payload = {
                    "method": "SUBSCRIBE",
                    "params": batch,
                    "id": i
                }
                await websocket.send(json.dumps(payload))
                await asyncio.sleep(0.5) # Jeda dikit biar server gak kaget
                print(f"Subs batch {i} sent...")
            
            print("✅ Listening for Candle Close events...")
            
            while True:
                msg = await websocket.recv()
                data = json.loads(msg)
                
                # Format Data Kline:
                # {'e': 'kline', 's': 'BTCUSDT', 'k': {'t': StartTime, 'c': Close, 'o': Open, 'x': IsClosed, ...}}
                
                if 'e' in data and data['e'] == 'kline':
                    kline = data['k']
                    is_closed = kline['x'] # True jika candle baru saja selesai
                    
                    if is_closed:
                        symbol_raw = data['s'] # BTCUSDT
                        # Ubah jadi BTC/USDT untuk display
                        symbol_formatted = f"{symbol_raw[:-4]}/{symbol_raw[-4:]}"
                        interval = kline['i'] # 8h atau 1d
                        
                        open_price = float(kline['o'])
                        close_price = float(kline['c'])
                        
                        if open_price == 0: continue
                        
                        # Hitung Persen Body Candle
                        pct = ((close_price - open_price) / open_price) * 100
                        
                        # Simpan ke Buffer
                        async with BUFFER_LOCK:
                            if interval in CLOSED_CANDLES_BUFFER:
                                CLOSED_CANDLES_BUFFER[interval].append({
                                    'symbol': symbol_formatted,
                                    'pct': pct,
                                    'tf': interval
                                })
                                # print(f"Buffered: {symbol_formatted} {interval} {pct:.2f}%")

        except websockets.ConnectionClosed:
            print("⚠️ WebSocket Disconnected! Reconnecting in 5s...")
            await asyncio.sleep(5)
        except Exception as e:
            print(f"Error: {e}")
            await asyncio.sleep(5)

async def main():
    # Jalankan Listener dan Reporter secara bersamaan (Parallel)
    await asyncio.gather(
        websocket_listener(),
        process_buffer_and_report()
    )

if __name__ == "__main__":
    print("🤖 Bot WebSocket Rank & Report Berjalan...")
    print("ℹ️ Menunggu Candle Close (8H / 1D) untuk Ranking...")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Bot Stopped.")


