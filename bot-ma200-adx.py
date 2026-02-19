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
from datetime import datetime

# ==========================================
# KONFIGURASI API & TELEGRAM
# ==========================================
# Pastikan API Key diisi agar bisa akses Futures
API_KEY = os.environ.get('BINANCE_API_KEY', 'fZwDMOfBL6rDU9jfUQox64fUAb2RSN48myxMPUGDAINYjmLdqJmUFhVRWLqlsX97')
API_SECRET = os.environ.get('BINANCE_API_SECRET', 'FmZNNbIOWIAddxVoLcNowLNW379E6gxyM85Bvy3QzlRMtK1eMApJp6vJtpGHWdWB')

# Telegram Config (Isi manual jika tidak pakai env var)
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', '8000712659:AAHltp77nGuakOzW9QMgQpVqnd5f1KgEsKA') 
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '-1003896189739')
 

# ==========================================
# KONFIGURASI STRATEGI
# ==========================================
TIMEFRAME = '15m'           # H1
ADX_THRESHOLD = 25         
LIMIT_CANDLES = 300        
TOP_COINS_COUNT = 20       # Jumlah koin yang dipantau

class HybridBot:
    def __init__(self):
        # Inisialisasi REST API Client (ccxt)
        self.exchange = ccxt.binance({
            'apiKey': BINANCE_API_KEY,
            'secret': BINANCE_SECRET_KEY,
            'enableRateLimit': True,
            'options': {'defaultType': 'future'}
        })
        
        # Data Store Lokal (Untuk pemantauan WebSocket cepat)
        self.local_data = {} 
        self.active_symbols = []
        
        print(f"✅ Hybrid Bot (WebSocket + REST Fallback) Siap.")
        self.send_telegram_text(f"🚀 **Hybrid Bot Started!**\nMonitoring: WebSocket\nCharting: REST API (High Precision)")

    # -----------------------------------------------------------
    # BAGIAN 1: FUNGSI UTILITAS & REST API (Untuk Data Bersih)
    # -----------------------------------------------------------
    def get_top_volume_pairs(self):
        """Mengambil Top Koin via REST API"""
        try:
            tickers = self.exchange.fetch_tickers()
            usdt_pairs = {k: v for k, v in tickers.items() if '/USDT' in k}
            sorted_pairs = sorted(usdt_pairs.items(), key=lambda x: x[1]['quoteVolume'], reverse=True)
            return [pair[0] for pair in sorted_pairs[:TOP_COINS_COUNT]]
        except Exception as e:
            print(f"⚠️ Gagal fetch pairs: {e}")
            return ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT']

    def fetch_rest_data(self, symbol):
        """
        Mengambil data bersih via REST API.
        Digunakan saat inisialisasi DAN saat mau bikin Chart (biar akurat).
        """
        try:
            ohlcv = self.exchange.fetch_ohlcv(symbol, TIMEFRAME, limit=LIMIT_CANDLES)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            return df
        except Exception as e:
            print(f"❌ REST API Error {symbol}: {e}")
            return None

    def calculate_indicators(self, df):
        """Menghitung Indikator Strategi (HLC3 + MA Dynamic + Pattern)"""
        # HLC3
        df['hlc3'] = (df['high'] + df['low'] + df['close']) / 3

        # MA200 Dynamic Zone
        df['ma200_hlc3'] = ta.sma(df['hlc3'], length=200)
        df['ma200_high'] = ta.sma(df['high'], length=200)
        df['ma200_low'] = ta.sma(df['low'], length=200)

        # Momentum
        df['ma20'] = ta.sma(df['hlc3'], length=20)
        df['ma10'] = ta.sma(df['hlc3'], length=10)

        # ADX
        adx = ta.adx(df['high'], df['low'], df['close'], length=14)
        df['adx'] = adx['ADX_14']
        
        # Slope (Kemiringan)
        df['slope_ma200'] = df['ma200_hlc3'].diff()
        df['slope_ma20'] = df['ma20'].diff()
        df['slope_ma10'] = df['ma10'].diff()
        
        return df

    def detect_pattern(self, curr, prev):
        """Deteksi Pin Bar & Engulfing"""
        body = abs(curr['close'] - curr['open'])
        upper_wick = curr['high'] - max(curr['close'], curr['open'])
        lower_wick = min(curr['close'], curr['open']) - curr['low']
        
        pattern = None
        # Pin Bar Logic
        if lower_wick >= (2 * body) and upper_wick <= (0.5 * body): pattern = "PINBAR_BULLISH"
        elif upper_wick >= (2 * body) and lower_wick <= (0.5 * body): pattern = "PINBAR_BEARISH"
        # Engulfing Logic
        if (curr['close'] > curr['open']) and (prev['close'] < prev['open']):
            if (curr['close'] > prev['open']) and (curr['open'] < prev['close']): pattern = "BULLISH_ENGULFING"
        elif (curr['close'] < curr['open']) and (prev['close'] > prev['open']):
            if (curr['close'] < prev['open']) and (curr['open'] > prev['close']): pattern = "BEARISH_ENGULFING"
        return pattern

    # -----------------------------------------------------------
    # BAGIAN 2: LOGIKA SINYAL & HANDLING ALERT (THREADED)
    # -----------------------------------------------------------
    def check_signal_logic(self, symbol, df):
        """Mengecek logika entry pada dataframe lokal (WebSocket)"""
        if len(df) < 205: return

        last = df.iloc[-1]
        prev = df.iloc[-2]
        pattern = self.detect_pattern(last, prev)
        
        if not pattern: return 

        price = last['close']
        signal = None
        
        # --- LOGIKA LONG ---
        trend_up = (price > last['ma200_hlc3']) and (last['slope_ma200'] > 0)
        struct_up = price > last['ma200_low']
        mom_up = (last['ma10'] > last['ma20']) and (last['slope_ma10'] > 0)
        adx_ok = last['adx'] > ADX_THRESHOLD
        pullback_up = last['low'] <= last['ma10']
        
        if trend_up and struct_up and mom_up and adx_ok and pullback_up and pattern in ["PINBAR_BULLISH", "BULLISH_ENGULFING"]:
            signal = "LONG 🟢"

        # --- LOGIKA SHORT ---
        trend_down = (price < last['ma200_hlc3']) and (last['slope_ma200'] < 0)
        struct_down = price < last['ma200_high']
        mom_down = (last['ma10'] < last['ma20']) and (last['slope_ma10'] < 0)
        adx_ok = last['adx'] > ADX_THRESHOLD
        pullback_down = last['high'] >= last['ma10']
        
        if trend_down and struct_down and mom_down and adx_ok and pullback_down and pattern in ["PINBAR_BEARISH", "BEARISH_ENGULFING"]:
            signal = "SHORT 🔴"

        # JIKA SINYAL VALID -> Panggil Thread terpisah untuk fetch REST API & Kirim Gambar
        if signal:
            print(f"⚡ WebSocket mendeteksi potensi {signal} di {symbol}. Memverifikasi dengan REST API...")
            # Kita jalankan di thread terpisah agar WebSocket tidak macet saat download data
            t = threading.Thread(target=self.process_verified_alert, args=(symbol, signal, pattern))
            t.start()

    def process_verified_alert(self, symbol, signal, pattern):
        """
        Fungsi ini dipanggil saat WebSocket menemukan sinyal.
        TUGAS: Ambil data bersih via REST API -> Buat Chart -> Kirim Telegram.
        """
        try:
            # 1. Ambil Data Fresh via REST API (Agar Chart Akurat & Tidak Eror)
            df_fresh = self.fetch_rest_data(symbol)
            
            if df_fresh is not None:
                # Hitung ulang indikator di data fresh
                df_fresh = self.calculate_indicators(df_fresh)
                last_data = df_fresh.iloc[-1]
                
                # 2. Buat Gambar Chart
                filename = f"chart_{symbol.replace('/','_')}_{int(time.time())}.png"
                self.generate_chart(df_fresh, symbol, f"{signal} - {pattern}", filename)
                
                # 3. Kirim ke Telegram
                msg = (
                    f"*{signal} SIGNAL VERIFIED*\n"
                    f"Asset: `{symbol}`\n"
                    f"Price: `{last_data['close']}`\n"
                    f"Trigger: `{pattern}`\n"
                    f"ADX: `{round(last_data['adx'], 2)}`\n"
                    f"Source: WebSocket Detect -> REST API Verify\n"
                )
                self.send_telegram_photo(msg, filename)
                print(f"✅ Alert {symbol} terkirim ke Telegram.")
            else:
                print(f"⚠️ Gagal fetch REST API untuk {symbol}, fallback ke data lokal tidak disarankan untuk charting.")
                
        except Exception as e:
            print(f"❌ Error di process_verified_alert: {e}")

    def generate_chart(self, df, symbol, title, filename):
        plot_df = df.tail(60).set_index('timestamp')
        mc = mpf.make_marketcolors(up='#089981', down='#F23645', inherit=True)
        s = mpf.make_mpf_style(base_mpf_style='nightclouds', marketcolors=mc)
        apds = [
            mpf.make_addplot(plot_df['ma200_hlc3'], color='white', width=1.5, panel=0),
            mpf.make_addplot(plot_df['ma10'], color='#FFD700', width=1, panel=0),
            mpf.make_addplot(plot_df['ma20'], color='#00E5FF', width=1, panel=0),
            mpf.make_addplot(plot_df['adx'], color='magenta', panel=1)
        ]
        mpf.plot(plot_df, type='candle', style=s, addplot=apds, title=title,
                 savefig=dict(fname=filename, dpi=100, bbox_inches='tight'), volume=False, panel_ratios=(6,2))

    # -----------------------------------------------------------
    # BAGIAN 3: WEBSOCKET ENGINE (Monitoring Cepat)
    # -----------------------------------------------------------
    def on_message(self, ws, message):
        try:
            json_msg = json.loads(message)
            # Cek event kline
            if 'e' in json_msg and json_msg['e'] == 'kline':
                kline = json_msg['k']
                if kline['x']: # HANYA JIKA CANDLE CLOSED
                    symbol_raw = json_msg['s']
                    symbol_fmt = symbol_raw[:-4] + "/USDT" # Format ulang ke ccxt style (BTC/USDT)
                    
                    # Update Dataframe Lokal (Untuk cek sinyal cepat)
                    new_row = {
                        'timestamp': pd.to_datetime(kline['t'], unit='ms'),
                        'open': float(kline['o']),
                        'high': float(kline['h']),
                        'low': float(kline['l']),
                        'close': float(kline['c']),
                        'volume': float(kline['v'])
                    }
                    
                    if symbol_fmt in self.local_data:
                        df = self.local_data[symbol_fmt]
                        df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
                        if len(df) > 305: df = df.iloc[1:] # Jaga memori
                        
                        # Hitung Indikator Lokal
                        df = self.calculate_indicators(df)
                        self.local_data[symbol_fmt] = df
                        
                        # Cek Sinyal di Data Lokal
                        self.check_signal_logic(symbol_fmt, df)
                        print(f"💧 WS Update: {symbol_fmt} Closed at {new_row['close']}")
        except Exception as e:
            print(f"WS Parse Error: {e}")

    def on_error(self, ws, error):
        print(f"Websocket Error: {error}")

    def on_close(self, ws, close_status_code, close_msg):
        print("Websocket Terputus. Mencoba reconnect dalam 5 detik...")
        time.sleep(5)
        self.run() # Restart

    def on_open(self, ws):
        print("✅ WebSocket Terhubung! Melakukan subscribe stream...")
        params = []
        for symbol in self.active_symbols:
            clean_symbol = symbol.replace('/', '').lower() # btc/usdt -> btcusdt
            params.append(f"{clean_symbol}@kline_{TIMEFRAME}")
            
        # Subscribe request
        ws.send(json.dumps({"method": "SUBSCRIBE", "params": params, "id": 1}))
        print(f"📡 Memantau {len(params)} pair secara real-time.")

    # -----------------------------------------------------------
    # BAGIAN 4: EKSEKUSI UTAMA
    # -----------------------------------------------------------
    def run(self):
        # 1. Ambil List Koin via REST
        print("Mengambil daftar Top Koin...")
        self.active_symbols = self.get_top_volume_pairs()
        
        # 2. Pre-load Data Historis via REST (Supaya indikator awal siap)
        print("Mengisi buffer data awal (REST API)...")
        for sym in self.active_symbols:
            df = self.fetch_rest_data(sym)
            if df is not None:
                df = self.calculate_indicators(df)
                self.local_data[sym] = df
                print(f"Load Data: {sym} OK")
            time.sleep(0.2) # Delay dikit biar gak kena limit

        # 3. Jalankan WebSocket (Futures)
        socket_url = "wss://fstream.binance.com/ws"
        ws = websocket.WebSocketApp(socket_url,
                                    on_open=self.on_open,
                                    on_message=self.on_message,
                                    on_error=self.on_error,
                                    on_close=self.on_close)
        ws.run_forever()

    def send_telegram_photo(self, message, image_path):
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
            with open(image_path, 'rb') as img:
                payload = {'chat_id': TELEGRAM_CHAT_ID, 'caption': message, 'parse_mode': 'Markdown'}
                files = {'photo': img}
                requests.post(url, data=payload, files=files)
            os.remove(image_path) # Hapus file setelah kirim
        except Exception as e: print(f"Telegram Fail: {e}")

    def send_telegram_text(self, message):
        try:
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", 
                          data={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"})
        except: pass

if __name__ == "__main__":
    try:
        bot = HybridBot()
        bot.run()
    except KeyboardInterrupt:
        print("Bot Stopped.")


