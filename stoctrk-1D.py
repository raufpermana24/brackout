import os
import time
import json
import threading
import requests
import ccxt
import pandas as pd
import pandas_ta as ta
import mplfinance as mpf
from datetime import datetime
from websocket import WebSocketApp
from concurrent.futures import ThreadPoolExecutor

# --- KONFIGURASI ENVIRONMENT ---
BINANCE_API_KEY = os.environ.get('BINANCE_API_KEY', 'fZwDMOfBL6rDU9jfUQox64fUAb2RSN48myxMPUGDAINYjmLdqJmUFhVRWLqlsX97')
BINANCE_SECRET_KEY = os.environ.get('BINANCE_API_SECRET', 'FmZNNbIOWIAddxVoLcNowLNW379E6gxyM85Bvy3QzlRMtK1eMApJp6vJtpGHWdWB')
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', '8000712659:AAHltp77nGuakOzW9QMgQpVqnd5f1KgEsKA')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '-1003812500986')

# --- PARAMETER TEKNIKAL ---
TIMEFRAMES = ['15m', '1h', '4h', '1d']
EMA_PERIOD = 50
STOCH_K = 14
STOCH_D = 3
STOCH_RSI_LEN = 14
MAX_COINS = 30 # Jumlah koin yang akan discan

class CryptoScannerBotFast:
    def __init__(self):
        self.exchange = ccxt.binance({
            'apiKey': BINANCE_API_KEY,
            'secret': BINANCE_SECRET_KEY,
            'options': {'defaultType': 'future'},
            'enableRateLimit': True,
        })
        self.data_store = {}
        # Thread pool untuk proses berat (charting & kirim telegram) agar WS tidak nge-hang
        self.executor = ThreadPoolExecutor(max_workers=10) 

    def log(self, msg):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

    # --- TELEGRAM UTILS ---
    def send_telegram_photo(self, photo_path, caption):
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
        try:
            with open(photo_path, 'rb') as photo:
                payload = {"chat_id": TELEGRAM_CHAT_ID, "caption": caption, "parse_mode": "Markdown"}
                files = {"photo": photo}
                requests.post(url, data=payload, files=files, timeout=10)
            self.log(f"✅ Sinyal terkirim ke Telegram: {caption.splitlines()[0]}")
        except Exception as e:
            self.log(f"❌ Gagal kirim Telegram: {e}")
        finally:
            if os.path.exists(photo_path):
                os.remove(photo_path) # Hapus file setelah dikirim

    # --- DATA ENGINE ---
    def fetch_historical_data(self, symbol, tf):
        """Mengambil data historis dengan limit yang pas untuk kalkulasi"""
        try:
            # Ambil 100 candle sudah cukup untuk EMA50 dan StochRSI
            bars = self.exchange.fetch_ohlcv(symbol, timeframe=tf, limit=100)
            # Hilangkan candle terakhir karena belum close (sedang berjalan)
            bars = bars[:-1] 
            df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            return symbol, tf, df
        except Exception as e:
            self.log(f"Error fetch {symbol} {tf}: {e}")
            return symbol, tf, None

    def prefetch_all_data(self, symbols):
        """Mengambil semua data historis SECARA PARALEL sebelum WS berjalan (Sangat Cepat)"""
        self.log("Memuat data historis untuk semua koin. Mohon tunggu...")
        tasks = []
        for symbol in symbols:
            self.data_store[symbol] = {}
            for tf in TIMEFRAMES:
                tasks.append((symbol, tf))
        
        # Eksekusi REST API secara parallel
        with ThreadPoolExecutor(max_workers=5) as rest_executor:
            results = rest_executor.map(lambda p: self.fetch_historical_data(*p), tasks)
            
        for sym, tf, df in results:
            if df is not None:
                self.data_store[sym][tf] = df
        self.log("✅ Data historis selesai dimuat!")

    def calculate_indicators(self, df):
        """Hitung EMA dan StochRSI"""
        # Tidak perlu copy() jika hanya mengoverwrite kolom
        df['ema50'] = ta.ema(df['close'], length=EMA_PERIOD)
        stoch_rsi = ta.stochrsi(df['close'], length=STOCH_RSI_LEN, rsi_length=STOCH_RSI_LEN, k=STOCH_K, d=STOCH_D)
        
        if stoch_rsi is not None and not stoch_rsi.empty:
            # Ambil 2 kolom pertama dari hasil stochrsi (biasanya stochk dan stochd)
            df['stoch_k'] = stoch_rsi.iloc[:, 0]
            df['stoch_d'] = stoch_rsi.iloc[:, 1]
        else:
            df['stoch_k'] = 0
            df['stoch_d'] = 0
        return df

    # --- BACKGROUND TASKS (TIDAK MEMBLOCK WEBSOCKET) ---
    def process_signal_and_send(self, df, symbol, tf, signal_type, price, k, d):
        """Fungsi ini dijalankan di background thread"""
        # 1. Buat Chart
        plot_df = df.tail(50).copy()
        plot_df.set_index('timestamp', inplace=True)
        filename = f"chart_{symbol.replace('/', '')}_{tf}_{int(time.time())}.png"
        
        apds = [
            mpf.make_addplot(plot_df['ema50'], color='orange', width=1.0),
            mpf.make_addplot(plot_df['stoch_k'], panel=1, color='blue'),
            mpf.make_addplot(plot_df['stoch_d'], panel=1, color='red'),
        ]
        
        try:
            mpf.plot(plot_df, type='candle', style='charles', addplot=apds,
                     title=f"{symbol} {tf} - {signal_type}", savefig=filename,
                     volume=False, figsize=(10, 7), panel_ratios=(6, 2))
            
            # 2. Kirim Telegram
            caption = (
                f"{signal_type} Terdeteksi!\n\n"
                f"Pair: #{symbol.replace('/', '')}\n"
                f"Timeframe: {tf}\n"
                f"Harga: {price}\n"
                f"StochRSI K: {k:.2f} | D: {d:.2f}"
            )
            self.send_telegram_photo(filename, caption)
        except Exception as e:
            self.log(f"Gagal memproses gambar {symbol}: {e}")

    # --- STRATEGY ENGINE ---
    def check_signal(self, symbol, tf, df):
        if len(df) < EMA_PERIOD + 2: return
        
        # Hitung indikator pada data yang sudah diupdate
        df = self.calculate_indicators(df)
        
        # Karena kita HANYA memproses saat candle tutup, 
        # maka iloc[-1] adalah candle yang baru saja tutup, 
        # dan iloc[-2] adalah candle tutup sebelumnya.
        curr = df.iloc[-1] 
        prev = df.iloc[-2] 
        
        price = curr['close']
        ema = curr['ema50']
        k, d = curr['stoch_k'], curr['stoch_d']
        pk, pd_val = prev['stoch_k'], prev['stoch_d']
        
        signal = None
        # Cek kondisi
        if price > ema and k < 20 and d < 20 and pk <= pd_val and k > d:
            signal = "🚀 *LONG*"
        elif price < ema and k > 80 and d > 80 and pk >= pd_val and k < d:
            signal = "🔻 *SHORT*"
            
        if signal:
            self.log(f"🔥 SINYAL {signal} di {symbol} TF {tf}")
            # Lemparkan pembuatan gambar dan pengiriman API ke Background Thread
            # agar WebSocket bisa lanjut menangani data lain tanpa nunggu!
            self.executor.submit(
                self.process_signal_and_send, 
                df.copy(), symbol, tf, signal, price, k, d
            )

    # --- WEBSOCKET HANDLERS ---
    def on_message(self, ws, message):
        data = json.loads(message)
        if 'k' not in data: return
        
        k = data['k']
        is_candle_closed = k['x'] # Flag True jika candle ditutup
        
        # OPTIMASI TERBESAR: 
        # Jangan lakukan apapun jika candle masih berjalan.
        # Hemat CPU 99% dibandingkan kode sebelumnya.
        if not is_candle_closed:
            return 
            
        symbol = data['s'] # cth: BTCUSDT
        tf = k['i']
        
        # Format simbol agar sesuai dengan format ccxt (BTC/USDT)
        if 'USDT' in symbol and '/' not in symbol:
            symbol_fmt = symbol.replace('USDT', '/USDT')
        else:
            symbol_fmt = symbol

        new_candle = {
            'timestamp': pd.to_datetime(k['t'], unit='ms'),
            'open': float(k['o']), 
            'high': float(k['h']), 
            'low': float(k['l']), 
            'close': float(k['c']), 
            'volume': float(k['v'])
        }
        
        if symbol_fmt in self.data_store and tf in self.data_store[symbol_fmt]:
            df = self.data_store[symbol_fmt][tf]
            
            # Tambahkan candle yang sudah close ke dataframe
            df.loc[len(df)] = new_candle
            
            # Buang data terlama agar memori tidak bocor (jaga max 100)
            if len(df) > 100:
                df = df.tail(100).reset_index(drop=True)
                self.data_store[symbol_fmt][tf] = df
            
            # Eksekusi strategi pengecekan sinyal
            self.check_signal(symbol_fmt, tf, df)

    def on_error(self, ws, error):
        self.log(f"WebSocket Error: {error}")

    def on_close(self, ws, close_status_code, close_msg):
        self.log("WebSocket Tertutup. Mencoba menghubungkan kembali dalam 5 detik...")
        time.sleep(5)
        self.run_websocket() # Auto reconnect

    def run_websocket(self):
        # Ambil top koin berdasarkan volume
        markets = self.exchange.fetch_tickers()
        sorted_symbols = sorted(
            [s for s in markets if '/USDT' in s and ':' not in s], # hindari format aneh
            key=lambda x: markets[x]['quoteVolume'], reverse=True
        )[:MAX_COINS]
        
        # Pre-fetch data historis via REST secara efisien
        self.prefetch_all_data(sorted_symbols)
        
        # Buat daftar stream
        streams = []
        for s in sorted_symbols:
            clean_symbol = s.replace('/', '').lower()
            for tf in TIMEFRAMES:
                streams.append(f"{clean_symbol}@kline_{tf}")
        
        stream_url = f"wss://fstream.binance.com/ws/{'/'.join(streams)}"
        self.log(f"Menghubungkan WebSocket untuk {len(sorted_symbols)} koin x {len(TIMEFRAMES)} TF...")
        
        ws = WebSocketApp(stream_url, 
                          on_message=self.on_message,
                          on_error=self.on_error,
                          on_close=self.on_close)
        ws.run_forever(ping_interval=30, ping_timeout=10) # Tambah ping agar koneksi stabil

    def start(self):
        self.log("Memulai Bot Crypto Fast Scanner...")
        self.run_websocket()

if __name__ == "__main__":
    bot = CryptoScannerBotFast()
    bot.start()
