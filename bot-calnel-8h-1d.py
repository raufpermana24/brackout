import asyncio
import ccxt.async_support as ccxt
import pandas as pd
import mplfinance as mpf
import requests
import os
import time
import random
from datetime import datetime, timedelta

# --- KONFIGURASI ENV ---
API_KEY = os.environ.get('BINANCE_API_KEY', 'fZwDMOfBL6rDU9jfUQox64fUAb2RSN48myxMPUGDAINYjmLdqJmUFhVRWLqlsX97')
API_SECRET = os.environ.get('BINANCE_API_SECRET', 'FmZNNbIOWIAddxVoLcNowLNW379E6gxyM85Bvy3QzlRMtK1eMApJp6vJtpGHWdWB')

# Telegram Config (Isi manual jika tidak pakai env var)
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', '8000712659:AAHltp77nGuakOzW9QMgQpVqnd5f1KgEsKA') 
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '-1003726025593')


# --- PENGATURAN KEAMANAN EKSTRA ---
TARGET_PERCENT = 2.5      
TIMEFRAMES = ['8h', '1d'] 
LIMIT_CANDLES = 150       

# [ULTRA SAFE CONFIG]
# Kurangi drastis kecepatan agar tidak dibanned
BATCH_SIZE = 5       # Hanya 5 request per batch
BATCH_DELAY = 4      # Istirahat 4 detik antar batch

# Memory
PROCESSED_CANDLES = {}

async def init_exchange():
    return ccxt.binance({
        'apiKey': API_KEY,
        'secret': API_SECRET,
        'enableRateLimit': True, 
        'options': {'defaultType': 'future'}
    })

# --- FUNGSI INDIKATOR & CHART ---
def calculate_indicators(df):
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
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).ewm(alpha=1/14, adjust=False).mean()
    loss = (-delta.where(delta < 0, 0)).ewm(alpha=1/14, adjust=False).mean()
    rs = gain / loss
    df['RSI'] = 100 - (100 / (1 + rs))
    return df

def send_telegram_alert(symbol, timeframe, change_pct, open_p, close_p, candle_ts, df):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return

    clean_symbol = symbol.replace('/', '').replace(':', '')
    file_path = f"safe_alert_{clean_symbol}_{timeframe}.png"
    
    try:
        plot_df = df.tail(60)
        
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
        
        mpf.plot(
            plot_df, type='candle', style=s, addplot=apd,
            title=f"\n{symbol} [{timeframe.upper()}] CLOSED\nBody: {change_pct:+.2f}%",
            ylabel='Price', volume=True, volume_panel=1, panel_ratios=(6, 2, 2), 
            savefig=file_path
        )

        trend = "🟢 BULLISH" if change_pct > 0 else "🔴 BEARISH"
        rsi_val = df['RSI'].iloc[-2]
        
        caption = (
            f"🛡️ **SAFE SCAN ALERT** 🛡️\n\n"
            f"{trend} CLOSE\n"
            f"Coin: `#{clean_symbol}`\n"
            f"Timeframe: **{timeframe.upper()}**\n"
            f"Time: {candle_ts}\n\n"
            f"Open: `{open_p}`\n"
            f"Close: `{close_p}`\n"
            f"Change: **{change_pct:+.2f}%**\n"
            f"RSI: {rsi_val:.1f}\n"
        )
        
        with open(file_path, 'rb') as photo:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
            data = {'chat_id': TELEGRAM_CHAT_ID, 'caption': caption, 'parse_mode': 'Markdown'}
            requests.post(url, files={'photo': photo}, data=data)
            
        print(f"🚀 Signal sent: {symbol}")
    except Exception as e:
        print(f"Error Telegram: {e}")
    finally:
        if os.path.exists(file_path): os.remove(file_path)

async def fetch_ohlcv_safe(exchange, symbol, timeframe):
    """
    Mengambil data dengan penanganan Error 418/429 yang ketat.
    """
    try:
        # Ambil data
        ohlcv = await exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=LIMIT_CANDLES)
        return ohlcv
    
    except ccxt.DDoSProtection as e:
        print(f"\n⛔ DDoS Protection Triggered: {e}")
        print("Tidur 2 menit...")
        await asyncio.sleep(120)
        return None
        
    except ccxt.RateLimitExceeded as e:
        print(f"\n⛔ Rate Limit Hit pada {symbol}! Tidur 1 menit...")
        await asyncio.sleep(60)
        return None
        
    except (ccxt.NetworkError, ccxt.ExchangeError) as e:
        # Cek pesan error string untuk kode 418/429 jika exception class tidak tertangkap
        err_msg = str(e).lower()
        if '418' in err_msg or '429' in err_msg or 'banned' in err_msg:
            print(f"\n💀 CRITICAL BAN DETECTED: {e}")
            print("🛑 Bot akan tidur selama 60 MENIT untuk pendinginan IP.")
            await asyncio.sleep(3600) # Tidur 1 Jam
            return None
            
        # Error ringan (timeout dll), abaikan saja untuk koin ini
        return None
        
    except Exception as e:
        return None

async def process_coin_logic(exchange, symbol, timeframe):
    ohlcv = await fetch_ohlcv_safe(exchange, symbol, timeframe)
    
    if not ohlcv or len(ohlcv) < 100: return None

    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    df.set_index('timestamp', inplace=True)

    closed_candle = df.iloc[-2]
    candle_ts = closed_candle.name
    
    # Idempotency Check
    key = (symbol, timeframe)
    if key in PROCESSED_CANDLES:
        if PROCESSED_CANDLES[key] == candle_ts:
            return None 

    open_p = float(closed_candle['open'])
    close_p = float(closed_candle['close'])
    
    if open_p == 0: return None
    change_pct = ((close_p - open_p) / open_p) * 100

    if abs(change_pct) >= TARGET_PERCENT:
        df = calculate_indicators(df)
        return {
            'symbol': symbol, 'tf': timeframe, 'pct': change_pct,
            'open': open_p, 'close': close_p, 'time': candle_ts, 'df': df
        }
    return None

async def run_bot():
    print("🤖 Menginisialisasi Bot Ultra Safe...")
    exchange = await init_exchange()
    
    try:
        # 1. Load Markets SEKALI SAJA di awal
        print("📥 Loading markets data...")
        markets = await exchange.load_markets()
        symbols = [s for s in markets if '/USDT' in s]
        print(f"📊 Total Pair: {len(symbols)}")
        
        while True:
            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 🛡️ Memulai Siklus Scan...")
            
            # Buat Task List
            all_tasks = []
            for sym in symbols:
                for tf in TIMEFRAMES:
                    all_tasks.append((sym, tf))
            
            total_tasks = len(all_tasks)
            
            # --- BATCH PROCESSING ---
            for i in range(0, total_tasks, BATCH_SIZE):
                batch = all_tasks[i : i + BATCH_SIZE]
                
                coroutines = [process_coin_logic(exchange, sym, tf) for sym, tf in batch]
                results = await asyncio.gather(*coroutines)
                
                valid_signals = [r for r in results if r is not None]
                for sig in valid_signals:
                    send_telegram_alert(
                        sig['symbol'], sig['tf'], sig['pct'], 
                        sig['open'], sig['close'], sig['time'], sig['df']
                    )
                    PROCESSED_CANDLES[(sig['symbol'], sig['tf'])] = sig['time']
                
                # Progress Log
                print(f"✅ Batch {i}/{total_tasks} ok. Sleep {BATCH_DELAY}s...", end='\r')
                
                # JEDA PENTING: Randomize delay agar terlihat natural
                sleep_time = BATCH_DELAY + random.uniform(0, 1) 
                await asyncio.sleep(sleep_time)
                
            print(f"\n🏁 Siklus Selesai. Menunggu 5 menit...")
            
            # Tunggu 5 menit sebelum scan ulang
            await asyncio.sleep(300)

    except Exception as e:
        print(f"Critical System Error: {e}")
    finally:
        await exchange.close()

if __name__ == "__main__":
    print("🛡️ Bot Anti-Ban Mode Aktif")
    print(f"📦 Batch Size: {BATCH_SIZE} | Delay: {BATCH_DELAY}s")
    
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        print("\nBot dimatikan pengguna.")



