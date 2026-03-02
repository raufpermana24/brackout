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

class CryptoScannerBot:
    def __init__(self):
        self.exchange = ccxt.binance({
            'apiKey': BINANCE_API_KEY,
            'secret': BINANCE_SECRET_KEY,
            'options': {'defaultType': 'future'},
            'enableRateLimit': True,
        })
        # Struktur data: data_store[symbol][tf] = DataFrame
        self.data_store = {}
        self.symbols = []
        self.is_ready = False

    def log(self, msg):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

    # --- TELEGRAM UTILS ---
    def send_telegram_photo(self, photo_path, caption):
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
        try:
            with open(photo_path, 'rb') as photo:
                payload = {"chat_id": TELEGRAM_CHAT_ID, "caption": caption, "parse_mode": "Markdown"}
                files = {"photo": photo}
                requests.post(url, data=payload, files=files)
        except Exception as e:
            self.log(f"Gagal kirim Telegram: {e}")

    # --- DATA ENGINE ---
    def fetch_historical_data(self, symbol, tf):
        """Ambil data dari REST API jika WebSocket belum lengkap"""
        try:
            bars = self.exchange.fetch_ohlcv(symbol, timeframe=tf, limit=100)
            df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            return df
        except:
            return None

    def calculate_indicators(self, df):
        """Hitung EMA dan StochRSI"""
        df = df.copy()
        df['ema50'] = ta.ema(df['close'], length=EMA_PERIOD)
        stoch_rsi = ta.stochrsi(df['close'], length=STOCH_RSI_LEN, rsi_length=STOCH_RSI_LEN, k=STOCH_K, d=STOCH_D)
        # Handle column naming from pandas_ta
        df = pd.concat([df, stoch_rsi], axis=1)
        df.columns = [*df.columns[:-2], 'stoch_k', 'stoch_d']
        return df

    # --- VISUALIZATION ---
    def create_screenshot(self, df, symbol, tf, signal_type):
        plot_df = df.tail(50).copy()
        plot_df.set_index('timestamp', inplace=True)
        filename = f"chart_{symbol.replace('/', '')}_{tf}.png"
        
        apds = [
            mpf.make_addplot(plot_df['ema50'], color='orange', width=1.0),
            mpf.make_addplot(plot_df['stoch_k'], panel=1, color='blue'),
            mpf.make_addplot(plot_df['stoch_d'], panel=1, color='red'),
        ]
        
        mpf.plot(plot_df, type='candle', style='charles', addplot=apds,
                 title=f"{symbol} {tf} - {signal_type}", savefig=filename,
                 volume=False, figsize=(10, 7), panel_ratios=(6, 2))
        return filename

    # --- STRATEGY ENGINE ---
    def check_signal(self, symbol, tf):
        df = self.data_store[symbol][tf]
        if len(df) < EMA_PERIOD + 2: return
        
        df = self.calculate_indicators(df)
        curr = df.iloc[-2] # Candle closed
        prev = df.iloc[-3] # Candle sebelumnya
        
        price = curr['close']
        ema = curr['ema50']
        k, d = curr['stoch_k'], curr['stoch_d']
        pk, pd_val = prev['stoch_k'], prev['stoch_d']
        
        signal = None
        if price > ema and k < 20 and d < 20 and pk <= pd_val and k > d:
            signal = "🚀 *LONG*"
        elif price < ema and k > 80 and d > 80 and pk >= pd_val and k < d:
            signal = "🔻 *SHORT*"
            
        if signal:
            self.log(f"SINYAL {signal} di {symbol} TF {tf}")
            caption = (
                f"{signal} Terdeteksi!\n\n"
                f"Pair: #{symbol.replace('/', '')}\n"
                f"Timeframe: {tf}\n"
                f"Harga: {price}\n"
                f"StochRSI K: {k:.2f} | D: {d:.2f}"
            )
            chart_file = self.create_screenshot(df, symbol, tf, signal)
            self.send_telegram_photo(chart_file, caption)
            if os.path.exists(chart_file): os.remove(chart_file)

    # --- WEBSOCKET HANDLERS ---
    def on_message(self, ws, message):
        data = json.loads(message)
        # Format kline dari binance: symbol, kline data
        k = data['k']
        symbol = data['s']
        tf = k['i']
        
        new_candle = [
            pd.to_datetime(k['t'], unit='ms'),
            float(k['o']), float(k['h']), float(k['l']), float(k['c']), float(k['v'])
        ]
        
        if symbol not in self.data_store: self.data_store[symbol] = {}
        
        # Inisialisasi data via REST jika belum ada
        if tf not in self.data_store[symbol]:
            df_hist = self.fetch_historical_data(symbol, tf)
            self.data_store[symbol][tf] = df_hist
        
        df = self.data_store[symbol][tf]
        
        # Update candle (replace if same timestamp, append if new)
        if df is not None:
            if new_candle[0] == df.iloc[-1]['timestamp']:
                df.iloc[-1] = new_candle
            else:
                df.loc[len(df)] = new_candle
                # Saat candle baru terbentuk, berarti candle sebelumnya CLOSED. Cek sinyal.
                self.check_signal(symbol, tf)
            
            # Keep only last 100
            if len(df) > 100: self.data_store[symbol][tf] = df.tail(100)

    def run_websocket(self):
        # Scan Top 30 koin volume tertinggi untuk efisiensi koneksi WS
        markets = self.exchange.fetch_tickers()
        sorted_symbols = sorted(
            [s for s in markets if '/USDT' in s], 
            key=lambda x: markets[x]['quoteVolume'], reverse=True
        )[:30]
        
        streams = []
        for s in sorted_symbols:
            clean_symbol = s.replace('/', '').split(':')[0].lower()
            for tf in TIMEFRAMES:
                streams.append(f"{clean_symbol}@kline_{tf}")
        
        stream_url = f"wss://fstream.binance.com/ws/{'/'.join(streams)}"
        self.log(f"Menghubungkan ke WebSocket untuk {len(sorted_symbols)} koin...")
        
        ws = WebSocketApp(stream_url, on_message=self.on_message)
        ws.run_forever()

    def start(self):
        self.log("Memulai Bot Multi-Timeframe (15m, 1h, 4h, 1d)...")
        # Jalankan WebSocket di thread terpisah
        ws_thread = threading.Thread(target=self.run_websocket)
        ws_thread.start()

if __name__ == "__main__":
    bot = CryptoScannerBot()
    bot.start()
