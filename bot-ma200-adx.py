import ccxt
import pandas as pd
import pandas_ta as ta
import mplfinance as mpf
import requests
import os
import json
import threading
import time
import websocket
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

# ==========================================
# 1. KONFIGURASI PRIBADI (WAJIB DIISI)
# ==========================================
BINANCE_API_KEY = os.environ.get('BINANCE_API_KEY', 'fZwDMOfBL6rDU9jfUQox64fUAb2RSN48myxMPUGDAINYjmLdqJmUFhVRWLqlsX97') 
BINANCE_SECRET_KEY = os.environ.get('BINANCE_API_SECRET', 'FmZNNbIOWIAddxVoLcNowLNW379E6gxyM85Bvy3QzlRMtK1eMApJp6vJtpGHWdWB')
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', '8000712659:AAHltp77nGuakOzW9QMgQpVqnd5f1KgEsKA') 
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '-1003896189739') 

# ==========================================
# 2. KONFIGURASI ENDPOINT
# ==========================================
# Endpoint WebSocket & REST API Binance Futures
WS_URL = "wss://fstream.binance.com/stream?streams="  # URL Default permintaan Anda
REST_URL = "https://fapi.binance.com"                 # URL REST API Futures

# ==========================================
# 3. KONFIGURASI STRATEGI & SYSTEM
# ==========================================
TIMEFRAME = '1h'           # H1
ADX_THRESHOLD = 40         # Filter Tren Kuat
LIMIT_CANDLES = 300        # Buffer Data Memory
MAX_WORKERS = 10           # Thread CPU (5-10 untuk VPS standar)

class BinanceFuturesBot:
    def __init__(self):
        # 1. Setup Exchange (REST API untuk Order/History)
        try:
            self.exchange = ccxt.binance({
                'apiKey': BINANCE_API_KEY,
                'secret': BINANCE_SECRET_KEY,
                'enableRateLimit': True,
                'options': {'defaultType': 'future'}
            })
            # Opsional: Jika ingin memaksa URL custom untuk ccxt (biasanya tidak perlu karena ccxt sudah handle)
            # self.exchange.urls['api']['fapiPublic'] = REST_URL 
            
            print(f"✅ Bot Started via CCXT & WebSocket.")
        except Exception as e:
            print(f"❌ Error Init: {e}")

        # 2. Memory Storage (Database RAM)
        self.local_data = {} 
        self.active_symbols = []
        
        # 3. Thread Pool (Otak Pemroses Paralel)
        self.executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
        
        # Kirim Notif Start
        self.send_telegram_text(f"🚀 **Bot Futures WebSocket Running!**\nStrategy: Candle Pattern + ADX > {ADX_THRESHOLD}")

    # =====================================================
    # BAGIAN 1: MANAJEMEN DATA (REST API)
    # =====================================================
    def get_all_usdt_pairs(self):
        """Mengambil semua pair USDT Futures"""
        try:
            tickers = self.exchange.fetch_tickers()
            pairs = [s for s in tickers.keys() if '/USDT' in s]
            print(f"📊 Market Loaded: {len(pairs)} Pairs.")
            return pairs
        except: return ['BTC/USDT', 'ETH/USDT']

    def fetch_initial_history(self, symbol):
        """Ambil 300 candle terakhir untuk isi awal Memory"""
        try:
            ohlcv = self.exchange.fetch_ohlcv(symbol, TIMEFRAME, limit=LIMIT_CANDLES)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            return df
        except: return None

    # =====================================================
    # BAGIAN 2: LOGIKA ANALISIS (THREAD WORKER)
    # =====================================================
    def calculate_indicators(self, df):
        # HLC3 & MA Zones
        df['hlc3'] = (df['high'] + df['low'] + df['close']) / 3
        df['ma200_hlc3'] = ta.sma(df['hlc3'], length=200)
        df['ma200_high'] = ta.sma(df['high'], length=200)
        df['ma200_low'] = ta.sma(df['low'], length=200)

        # Momentum & ADX
        df['ma20'] = ta.sma(df['hlc3'], length=20)
        df['ma10'] = ta.sma(df['hlc3'], length=10)
        adx = ta.adx(df['high'], df['low'], df['close'], length=14)
        df['adx'] = adx['ADX_14']
        
        # Slope
        df['slope_ma200'] = df['ma200_hlc3'].diff()
        df['slope_ma10'] = df['ma10'].diff()
        
        return df

    def detect_pattern(self, curr, prev):
        body = abs(curr['close'] - curr['open'])
        upper_wick = curr['high'] - max(curr['close'], curr['open'])
        lower_wick = min(curr['close'], curr['open']) - curr['low']
        if body == 0: return None

        # Pin Bar
        if lower_wick >= (2 * body) and upper_wick <= (0.5 * body): return "PINBAR_BULLISH"
        if upper_wick >= (2 * body) and lower_wick <= (0.5 * body): return "PINBAR_BEARISH"
        
        # Engulfing
        if (curr['close'] > curr['open']) and (prev['close'] < prev['open']):
            if (curr['close'] > prev['open']) and (curr['open'] < prev['close']): return "BULLISH_ENGULFING"
        if (curr['close'] < curr['open']) and (prev['close'] > prev['open']):
            if (curr['close'] < prev['open']) and (curr['open'] > prev['close']): return "BEARISH_ENGULFING"
            
        return None

    def process_data_logic(self, symbol, df):
        """Fungsi ini berjalan di background thread"""
        try:
            if len(df) < 205: return

            # 1. Hitung Indikator
            df = self.calculate_indicators(df)
            self.local_data[symbol] = df # Update Memory Utama

            last = df.iloc[-1]
            prev = df.iloc[-2]

            # 2. Filter ADX (Hemat resource: jika tren lemah, stop)
            if last['adx'] <= ADX_THRESHOLD: return

            # 3. Cek Pattern Candle
            pattern = self.detect_pattern(last, prev)
            if not pattern: return

            # 4. Cek Konfluensi (Trend + Momentum + Support/Resist)
            price = last['close']
            signal = None

            # Logic Long
            if pattern in ["PINBAR_BULLISH", "BULLISH_ENGULFING"]:
                if (price > last['ma200_hlc3']) and (last['slope_ma200'] > 0) and \
                   (price > last['ma200_low']) and \
                   (last['ma10'] > last['ma20']) and (last['slope_ma10'] > 0) and \
                   (last['low'] <= last['ma10']): # Pullback
                    signal = "LONG 🟢"

            # Logic Short
            elif pattern in ["PINBAR_BEARISH", "BEARISH_ENGULFING"]:
                if (price < last['ma200_hlc3']) and (last['slope_ma200'] < 0) and \
                   (price < last['ma200_high']) and \
                   (last['ma10'] < last['ma20']) and (last['slope_ma10'] < 0) and \
                   (last['high'] >= last['ma10']): # Pullback
                    signal = "SHORT 🔴"

            # 5. Eksekusi Sinyal
            if signal:
                print(f"🔥 SIGNAL: {symbol} | {signal}")
                # Kirim ke Thread Alerting
                self.send_alert(symbol, signal, pattern, last, df)

        except Exception as e: pass

    def send_alert(self, symbol, signal, pattern, data, df):
        """Generate Chart & Kirim Telegram"""
        try:
            filename = f"chart_{symbol.replace('/','')}_{int(time.time())}.png"
            
            # Setup Chart Style
            plot_df = df.tail(60).set_index('timestamp')
            mc = mpf.make_marketcolors(up='#089981', down='#F23645', inherit=True)
            s = mpf.make_mpf_style(base_mpf_style='nightclouds', marketcolors=mc)
            
            apds = [
                mpf.make_addplot(plot_df['ma200_hlc3'], color='white', width=1.5, panel=0),
                mpf.make_addplot(plot_df['ma10'], color='#FFD700', width=1, panel=0),
                mpf.make_addplot(plot_df['ma20'], color='#00E5FF', width=1, panel=0),
                mpf.make_addplot(plot_df['adx'], color='magenta', panel=1)
            ]
            
            # Render Gambar
            mpf.plot(plot_df, type='candle', style=s, addplot=apds, title=f"{symbol} {signal}",
                     savefig=dict(fname=filename, dpi=100, bbox_inches='tight'), volume=False, panel_ratios=(6,2))
            
            # Kirim Telegram
            caption = (
                f"*{signal} SIGNAL DETECTED*\n"
                f"Asset: `{symbol}`\n"
                f"Price: `{data['close']}`\n"
                f"Pattern: `{pattern}`\n"
                f"ADX: `{round(data['adx'], 2)}`\n"
                f"Source: WebSocket Realtime"
            )
            
            with open(filename, 'rb') as img:
                requests.post(
                    f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto",
                    data={'chat_id': TELEGRAM_CHAT_ID, 'caption': caption, 'parse_mode': 'Markdown'},
                    files={'photo': img}
                )
            os.remove(filename) # Hapus file
            
        except Exception as e: print(f"Alert Error: {e}")

    # =====================================================
    # BAGIAN 3: WEBSOCKET ENGINE (CORE)
    # =====================================================
    def on_message(self, ws, message):
        """Menangkap Data Realtime"""
        try:
            json_msg = json.loads(message)
            # Cek apakah tipe data adalah Kline/Candle
            if 'e' in json_msg and json_msg['e'] == 'kline':
                k = json_msg['k']
                
                # HANYA PROSES SAAT CANDLE CLOSE (x = True)
                if k['x']:
                    symbol_raw = json_msg['s'] # BTCUSDT
                    symbol_fmt = symbol_raw[:-4] + "/USDT" # BTC/USDT
                    
                    new_row = {
                        'timestamp': pd.to_datetime(k['t'], unit='ms'),
                        'open': float(k['o']), 'high': float(k['h']),
                        'low': float(k['l']), 'close': float(k['c']),
                        'volume': float(k['v'])
                    }
                    
                    # Update Memory DataFrame
                    if symbol_fmt in self.local_data:
                        df = self.local_data[symbol_fmt]
                        df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
                        
                        # Jaga ukuran memori (Hapus data lama)
                        if len(df) > LIMIT_CANDLES: df = df.iloc[1:]
                        
                        # Lempar ke Thread Pool untuk dianalisis
                        # .copy() penting agar data thread-safe
                        self.executor.submit(self.process_data_logic, symbol_fmt, df.copy())
                        
        except Exception: pass

    def on_error(self, ws, error):
        print(f"⚠️ WS Error: {error}")

    def on_close(self, ws, close_status_code, close_msg):
        print("🔌 WebSocket Disconnected.")

    def on_open(self, ws):
        print("📡 WebSocket Connected! Subscribing streams...")
        
        # Batch Subscribe (Mencegah Request Limit)
        batch_size = 50
        all_params = []
        
        for symbol in self.active_symbols:
            clean = symbol.replace('/', '').lower()
            all_params.append(f"{clean}@kline_{TIMEFRAME}")
            
        # Kirim per batch
        for i in range(0, len(all_params), batch_size):
            batch = all_params[i:i + batch_size]
            payload = {"method": "SUBSCRIBE", "params": batch, "id": i+1}
            ws.send(json.dumps(payload))
            time.sleep(0.5)
            
        print(f"✅ Subscribed to {len(all_params)} streams.")

    def run(self):
        # 1. Ambil List Semua Koin
        self.active_symbols = self.get_all_usdt_pairs()
        
        # 2. Isi Data Awal (Warming Up Memory)
        print("⏳ Loading initial history (Rest API)...")
        count = 0
        for sym in self.active_symbols:
            df = self.fetch_initial_history(sym)
            if df is not None:
                # Pre-calculate indicator
                df = self.calculate_indicators(df)
                self.local_data[sym] = df
                count += 1
                print(f"\rLoaded {count}/{len(self.active_symbols)}", end="", flush=True)
            time.sleep(0.05) # Delay dikit biar aman
            
        print("\n✅ Memory Ready. Starting WebSocket Loop...")

        # 3. Main Loop (Auto Reconnect)
        while True:
            try:
                # Menentukan URL WebSocket
                # Jika WS_URL di set ke "stream?streams=" tapi kita menggunakan metode SUBSCRIBE JSON,
                # kita harus menggunakan base endpoint /ws agar tidak error saat handshake.
                target_ws_url = WS_URL
                if "stream?streams=" in target_ws_url and target_ws_url.endswith("="):
                    target_ws_url = "wss://fstream.binance.com/ws"
                
                # Hubungkan ke Binance Futures Stream
                ws = websocket.WebSocketApp(
                    target_ws_url,
                    on_open=self.on_open,
                    on_message=self.on_message,
                    on_error=self.on_error,
                    on_close=self.on_close
                )
                # Jalankan dengan Ping Interval 60 detik (Anti Putus)
                ws.run_forever(ping_interval=60, ping_timeout=10)
                
            except Exception as e:
                print(f"Critical Error: {e}")
            
            print("🔄 Reconnecting in 5 seconds...")
            time.sleep(5)

    def send_telegram_text(self, message):
        try:
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", 
                          data={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"})
        except: pass

if __name__ == "__main__":
    try:
        bot = BinanceFuturesBot()
        bot.run()
    except KeyboardInterrupt:
        print("Bot Stopped.")



