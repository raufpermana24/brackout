import asyncio
import ccxt.async_support as ccxt
import pandas as pd
import mplfinance as mpf
import requests
import os
import time
from datetime import datetime, timedelta

# --- KONFIGURASI API ---
API_KEY = os.environ.get('BINANCE_API_KEY', 'fZwDMOfBL6rDU9jfUQox64fUAb2RSN48myxMPUGDAINYjmLdqJmUFhVRWLqlsX97')
API_SECRET = os.environ.get('BINANCE_API_SECRET', 'FmZNNbIOWIAddxVoLcNowLNW379E6gxyM85Bvy3QzlRMtK1eMApJp6vJtpGHWdWB')

# Telegram Config (Isi manual jika tidak pakai env var)
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', '8000712659:AAHltp77nGuakOzW9QMgQpVqnd5f1KgEsKA') 
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '-1003726025593')


# --- SETTING SCANNER ---
TARGET_PERCENT = 2.5       # Target: Body candle minimal 2.5%
TIMEFRAMES = ['8h', '1d']  # Timeframe yang dipantau
LIMIT_CANDLES = 200        # Ambil 200 data (aman untuk MA100)
MAX_CONCURRENT_REQ = 10    # Batas koneksi parallel

# --- MEMORY DATABASE ---
# Menyimpan timestamp candle yang sudah dilaporkan agar tidak dikirim ulang
# Format: {(Symbol, Timeframe): Timestamp_Candle}
PROCESSED_CANDLES = {}

async def get_exchange():
    return ccxt.binance({
        'apiKey': API_KEY,
        'secret': API_SECRET,
        'enableRateLimit': True,
        'options': {'defaultType': 'future'}
    })

def calculate_rsi(series, period=14):
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).ewm(alpha=1/period, adjust=False).mean()
    loss = (-delta.where(delta < 0, 0)).ewm(alpha=1/period, adjust=False).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def add_indicators(df):
    # Moving Averages
    df['MA5'] = df['close'].rolling(window=5).mean()
    df['MA10'] = df['close'].rolling(window=10).mean()
    df['MA30'] = df['close'].rolling(window=30).mean()
    df['MA100'] = df['close'].rolling(window=100).mean()
    
    # Bollinger Bands
    df['BB_Mid'] = df['close'].rolling(window=20).mean()
    df['BB_Std'] = df['close'].rolling(window=20).std()
    df['BB_Upper'] = df['BB_Mid'] + (2 * df['BB_Std'])
    df['BB_Lower'] = df['BB_Mid'] - (2 * df['BB_Std'])
    
    # RSI
    df['RSI'] = calculate_rsi(df['close'])
    return df

def send_telegram_alert(symbol, timeframe, change_pct, open_p, close_p, candle_time, df):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return

    clean_symbol = symbol.replace('/', '').replace(':', '')
    file_path = f"alert_{clean_symbol}_{timeframe}.png"
    
    try:
        # Visualisasi 60 candle terakhir
        plot_df = df.tail(60)
        
        # Konfigurasi Tampilan Chart
        apd = [
            mpf.make_addplot(plot_df['MA5'], color='blue', width=0.8, panel=0),
            mpf.make_addplot(plot_df['MA10'], color='orange', width=0.8, panel=0),
            mpf.make_addplot(plot_df['MA30'], color='purple', width=1.0, panel=0),
            mpf.make_addplot(plot_df['MA100'], color='black', width=1.2, panel=0),
            mpf.make_addplot(plot_df['BB_Upper'], color='gray', width=0.5, linestyle='--', panel=0),
            mpf.make_addplot(plot_df['BB_Lower'], color='gray', width=0.5, linestyle='--', panel=0),
            mpf.make_addplot(plot_df['RSI'], panel=2, color='#8e44ad', width=1.5, ylabel='RSI'),
            mpf.make_addplot([70]*len(plot_df), panel=2, color='red', width=0.5, linestyle='--'),
            mpf.make_addplot([30]*len(plot_df), panel=2, color='green', width=0.5, linestyle='--'),
        ]
        
        mc = mpf.make_marketcolors(up='#2ebd85', down='#f6465d', edge='inherit', wick='inherit', volume='in')
        s  = mpf.make_mpf_style(marketcolors=mc, gridstyle='--', y_on_right=True)
        
        # Judul Chart
        title_text = f"\n{symbol} [{timeframe.upper()}] CLOSED\nBody Change: {change_pct:+.2f}%"
        
        mpf.plot(
            plot_df, type='candle', style=s, addplot=apd,
            title=title_text, ylabel='Price', 
            volume=True, volume_panel=1, panel_ratios=(6, 2, 2), 
            savefig=file_path
        )

        # Siapkan Caption Telegram
        trend = "LONG/BUY 🟢" if change_pct > 0 else "SHORT/SELL 🔴"
        rsi_val = df['RSI'].iloc[-2] # RSI saat candle close
        
        caption = (
            f"🔔 **CANDLE CLOSED ALERT** 🔔\n\n"
            f"{trend}\n"
            f"Symbol: `#{clean_symbol}`\n"
            f"Timeframe: **{timeframe.upper()}**\n"
            f"Time: {candle_time}\n\n"
            f"📊 **Candle Data:**\n"
            f"Open: `{open_p}`\n"
            f"Close: `{close_p}`\n"
            f"Change: **{change_pct:+.2f}%**\n\n"
            f"📈 **Indicator:**\n"
            f"RSI: {rsi_val:.1f}\n"
            f"Vol: {df['volume'].iloc[-2]:.0f}"
        )
        
        with open(file_path, 'rb') as photo:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
            data = {'chat_id': TELEGRAM_CHAT_ID, 'caption': caption, 'parse_mode': 'Markdown'}
            requests.post(url, files={'photo': photo}, data=data)
            
        print(f"✅ Alert sent for {symbol} ({timeframe})")
        
    except Exception as e:
        print(f"❌ Error sending image: {e}")
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)

async def process_coin(exchange, semaphore, symbol, timeframe):
    async with semaphore:
        try:
            # 1. Ambil Data OHLCV
            ohlcv = await exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=LIMIT_CANDLES)
            if not ohlcv or len(ohlcv) < 100: return None

            # 2. Buat DataFrame
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            df.set_index('timestamp', inplace=True)

            # ---------------------------------------------------------
            # LOGIKA UTAMA: MENGHITUNG BODY CANDLE YANG SUDAH CLOSED
            # ---------------------------------------------------------
            
            # iloc[-1] = Candle yang sedang berjalan (Running/Unfinished)
            # iloc[-2] = Candle yang sudah selesai (Closed/Final)
            
            closed_candle = df.iloc[-2]
            
            # Ambil timestamp candle tersebut (Waktu Open candle)
            candle_ts = closed_candle.name 
            
            # CEK IDEMPOTENCY: Apakah candle ini sudah pernah dikirim?
            mem_key = (symbol, timeframe)
            if mem_key in PROCESSED_CANDLES:
                if PROCESSED_CANDLES[mem_key] == candle_ts:
                    return None # Sudah dikirim, skip!

            # Ambil Harga OPEN dan CLOSE dari candle yang sudah closed
            open_price = float(closed_candle['open'])
            close_price = float(closed_candle['close'])
            
            if open_price == 0: return None

            # HITUNG PERSENTASE (Body Candle)
            # Rumus: ((Close - Open) / Open) * 100
            change_pct = ((close_price - open_price) / open_price) * 100

            # ---------------------------------------------------------

            # Cek apakah memenuhi target 2.5%
            if abs(change_pct) >= TARGET_PERCENT:
                
                # Hitung indikator untuk visualisasi chart
                df = add_indicators(df)
                
                # Return data lengkap untuk dikirim
                return {
                    'symbol': symbol,
                    'tf': timeframe,
                    'pct': change_pct,
                    'open': open_price,
                    'close': close_price,
                    'time': candle_ts,
                    'df': df
                }
            
            # Jika tidak sinyal, tetap simpan timestamp agar tidak dicek ulang (Opsional)
            # PROCESSED_CANDLES[mem_key] = candle_ts 

        except Exception as e:
            # Error connection, skip
            return None
    return None

async def scanner_job():
    exchange = await get_exchange()
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 🔍 Scanning Closed Candles...")
    
    try:
        markets = await exchange.load_markets()
        symbols = [s for s in markets if '/USDT' in s]
        
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQ)
        tasks = []

        # Buat task untuk setiap koin & setiap timeframe
        for sym in symbols:
            for tf in TIMEFRAMES:
                tasks.append(process_coin(exchange, semaphore, sym, tf))
        
        # Jalankan parallel
        results = await asyncio.gather(*tasks)
        
        # Filter hasil yang None
        valid_signals = [r for r in results if r is not None]
        print(f"found {len(valid_signals)} signals.")

        for signal in valid_signals:
            # Kirim Telegram
            send_telegram_alert(
                symbol=signal['symbol'],
                timeframe=signal['tf'],
                change_pct=signal['pct'],
                open_p=signal['open'],
                close_p=signal['close'],
                candle_time=signal['time'],
                df=signal['df']
            )
            
            # Update Memory: Tandai candle ini sudah diproses
            key = (signal['symbol'], signal['tf'])
            PROCESSED_CANDLES[key] = signal['time']
            
            time.sleep(1) # Delay pengiriman

    except Exception as e:
        print(f"Critical error: {e}")
    finally:
        await exchange.close()

if __name__ == "__main__":
    print("🤖 Bot Active: Scanning Close Prices (Index -2)")
    print(f"Target: +/- {TARGET_PERCENT}% Body Candle")
    
    while True:
        asyncio.run(scanner_job())
        
        # Interval Scan: 5 Menit (300 detik)
        # Aman karena kita menggunakan filter timestamp (tidak akan double post)
        print("⏳ Waiting 5 minutes...")
        time.sleep(300)


