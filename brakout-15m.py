import ccxt
import pandas as pd
import pandas_ta as ta
import numpy as np
import time
import requests
import matplotlib.pyplot as plt
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

# ==========================================
# KONFIGURASI BOT & TELEGRAM
# ==========================================
API_KEY = 'IbZV3RoXl5ErnYaNT2tLwT3OtvNK9Gy9vgR11QIaL36or8WCZT6TQAItaTXZ4IOc'  # Biarkan kosong jika hanya scan publik
API_SECRET = 'ks4gpwvQkVVWxM6AKkxkcjD1x9ZO1vtyTwMYD5sGJyW8oraJ1Z1IM6kfQyeOSOEB'

# Konfigurasi Telegram
TELEGRAM_TOKEN = '8000712659:AAHltp77nGuakOzW9QMgQpVqnd5f1KgEsKA'
TELEGRAM_CHAT_ID = '-1003842052901'

# Parameter Scanner
TIMEFRAME = '15m'       # Timeframe (15m, 1h, 4h)
LIMIT_CANDLES = 200    # Jumlah candle yang dianalisis
TOP_COINS_COUNT = 300  # Jumlah koin yang di-scan
VOLUME_THRESHOLD = 2.0 # Z-Score Volume

# File untuk menyimpan riwayat sinyal agar tidak double
HISTORY_FILE = 'scan_history.json'

class CryptoScanner:
    def __init__(self):
        try:
            self.exchange = ccxt.binance({
                'apiKey': API_KEY,
                'secret': API_SECRET,
                'options': {'defaultType': 'future'},
                'enableRateLimit': True
            })
            print("✅ Terhubung ke Binance Futures")
        except Exception as e:
            print(f"❌ Gagal terhubung: {e}")
            exit()

    def get_top_volume_pairs(self, limit=TOP_COINS_COUNT):
        """Mengambil pasangan koin dengan volume tertinggi"""
        print(f"🔄 Mengambil {limit} koin teratas...")
        try:
            tickers = self.exchange.fetch_tickers()
            pairs = [
                symbol for symbol, data in tickers.items() 
                if '/USDT' in symbol and 'BUSD' not in symbol and 'USDC' not in symbol
            ]
            sorted_pairs = sorted(pairs, key=lambda x: tickers[x]['quoteVolume'], reverse=True)
            return sorted_pairs[:limit]
        except Exception as e:
            print(f"❌ Error fetching tickers: {e}")
            return []

    def calculate_indicators(self, df):
        """
        Inti dari Logika 'AI' dan Algoritma (Tidak Diubah).
        """
        # 1. Donchian Channels
        df['DC_Upper'] = df['high'].rolling(window=20).max()
        df['DC_Lower'] = df['low'].rolling(window=20).min()
        
        # 2. Volume Z-Score
        vol_mean = df['volume'].rolling(window=20).mean()
        vol_std = df['volume'].rolling(window=20).std()
        df['Vol_ZScore'] = (df['volume'] - vol_mean) / vol_std
        
        # 3. Money Flow
        df['MFM'] = ((df['close'] - df['low']) - (df['high'] - df['close'])) / (df['high'] - df['low'])
        df['Net_Volume_Flow'] = df['MFM'] * df['volume']
        
        # 4. Trend Filter
        df['EMA_200'] = ta.ema(df['close'], length=200)

        return df

    def analyze_pair(self, symbol):
        """Menganalisa satu pasangan koin"""
        try:
            bars = self.exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=LIMIT_CANDLES)
            if not bars or len(bars) < 50:
                return None
            
            df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            
            df = self.calculate_indicators(df)
            
            last_row = df.iloc[-1]
            prev_row = df.iloc[-2]
            
            signal = None
            strength = "Netral"
            
            # --- LOGIKA SIGNAL ---
            volume_breakout = last_row['Vol_ZScore'] > VOLUME_THRESHOLD
            price_breakout_up = last_row['close'] >= prev_row['DC_Upper'] or last_row['high'] > prev_row['DC_Upper']
            price_breakout_down = last_row['close'] <= prev_row['DC_Lower'] or last_row['low'] < prev_row['DC_Lower']
            strong_buying_pressure = last_row['Net_Volume_Flow'] > 0
            
            # --- PENENTUAN POSISI ---
            if price_breakout_up and volume_breakout and strong_buying_pressure:
                signal = "LONG 🟢" 
                strength = f"Z:{last_row['Vol_ZScore']:.1f}"
            
            elif price_breakout_down and volume_breakout and not strong_buying_pressure:
                signal = "SHORT 🔴"
                strength = f"Z:{last_row['Vol_ZScore']:.1f}"
                
            if signal:
                # Mengembalikan timestamp sebagai string unik untuk identifikasi candle
                candle_time_str = str(last_row['timestamp'])
                return {
                    'Pair': symbol.replace('/USDT', ''), 
                    'Price': last_row['close'],
                    'Signal': signal,
                    'Vol': strength,
                    'TimeID': candle_time_str # ID Unik untuk mencegah duplikat
                }
                
        except Exception:
            return None
        return None

    def send_telegram_alert(self, message, image_path=None):
        """Mengirim pesan dan gambar ke Telegram"""
        if TELEGRAM_TOKEN == 'ISI_TOKEN_BOT_TELEGRAM_DISINI':
            print("⚠️ Token Telegram belum diisi. Lewati pengiriman.")
            return

        url_msg = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        url_photo = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
        
        try:
            # Kirim Pesan Teks
            requests.post(url_msg, data={'chat_id': TELEGRAM_CHAT_ID, 'text': message})
            
            # Kirim Gambar (Screenshot Table)
            if image_path:
                with open(image_path, 'rb') as photo:
                    requests.post(url_photo, data={'chat_id': TELEGRAM_CHAT_ID}, files={'photo': photo})
            print("✅ Notifikasi Telegram terkirim!")
        except Exception as e:
            print(f"❌ Gagal kirim Telegram: {e}")

    def generate_image_table(self, df):
        """Membuat gambar PNG dari DataFrame hasil scan"""
        try:
            # Hapus kolom TimeID agar tidak muncul di gambar
            plot_df = df.drop(columns=['TimeID'], errors='ignore')
            
            fig, ax = plt.subplots(figsize=(8, len(plot_df) * 0.6 + 1)) 
            ax.axis('tight')
            ax.axis('off')
            
            colors = []
            for signal in plot_df['Signal']:
                if 'LONG' in signal:
                    colors.append(['#d4edda', '#d4edda', '#d4edda', '#d4edda']) 
                else:
                    colors.append(['#f8d7da', '#f8d7da', '#f8d7da', '#f8d7da']) 

            table = ax.table(cellText=plot_df.values, colLabels=plot_df.columns, loc='center', cellLoc='center', cellColours=colors)
            table.auto_set_font_size(False)
            table.set_fontsize(12)
            table.scale(1.2, 1.5)
            
            plt.title(f"CRYPTO AI SIGNAL - {TIMEFRAME}\n{datetime.now().strftime('%H:%M %d/%m')}", fontsize=14, weight='bold')
            
            filename = "scan_result.png"
            plt.savefig(filename, bbox_inches='tight', dpi=150)
            plt.close()
            return filename
        except Exception as e:
            print(f"❌ Gagal membuat gambar: {e}")
            return None

    # --- FUNGSI BARU UNTUK MENCEGAH ANALISIS DOUBLE ---
    def load_processed_signals(self):
        """Membaca history sinyal yang sudah dikirim"""
        if os.path.exists(HISTORY_FILE):
            try:
                with open(HISTORY_FILE, 'r') as f:
                    return json.load(f)
            except:
                return {}
        return {}

    def save_processed_signals(self, history):
        """Menyimpan history sinyal baru"""
        with open(HISTORY_FILE, 'w') as f:
            json.dump(history, f)

    def run_scan(self):
        print(f"\n🚀 Memulai Scan Cepat pada {TOP_COINS_COUNT} koin. Timeframe: {TIMEFRAME}")
        
        # 1. Load History Lama
        history = self.load_processed_signals()
        
        pairs = self.get_top_volume_pairs()
        raw_results = []
        
        # Scan paralel
        with ThreadPoolExecutor(max_workers=20) as executor:
            futures = [executor.submit(self.analyze_pair, pair) for pair in pairs]
            
            total = len(pairs)
            for i, future in enumerate(futures):
                res = future.result()
                if res:
                    raw_results.append(res)
                if i % 50 == 0:
                    print(f"⏳ Progress: {i}/{total}...")
        
        # 2. Filter Hasil: Hanya ambil yang BELUM ada di history
        new_signals = []
        current_time_key = datetime.now().strftime('%Y-%m-%d') # Key grouping harian (opsional)
        
        print("\n🔍 Memeriksa duplikasi sinyal...")
        
        for res in raw_results:
            # ID Unik = Nama Pair + Signal + Waktu Candle
            # Contoh: BTCUSDT_LONG_2023-10-27 10:00:00
            unique_id = f"{res['Pair']}_{res['Signal']}_{res['TimeID']}"
            
            if unique_id not in history:
                # Sinyal Baru!
                new_signals.append(res)
                history[unique_id] = True # Tandai sebagai sudah diproses
            else:
                print(f"   ⏩ Skip {res['Pair']} (Sudah dikirim sebelumnya)")

        # Bersihkan history yang terlalu lama (opsional, reset manual jika file terlalu besar)
        # Disini kita simpan history yang sudah diupdate
        self.save_processed_signals(history)

        print("\n" + "="*60)
        print(f"HASIL SCAN BARU - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print("="*60)
        
        if len(new_signals) > 0:
            df_res = pd.DataFrame(new_signals)
            # Tampilkan di console (tanpa kolom TimeID biar rapi)
            print(df_res.drop(columns=['TimeID']).to_string(index=False))
            
            # Buat Gambar
            image_file = self.generate_image_table(df_res)
            
            # Kirim Telegram
            msg = f"🤖 **BOT SIGNAL DETECTED** 🤖\n\nFound {len(new_signals)} NEW opportunities."
            self.send_telegram_alert(msg, image_file)
            
        else:
            print("Tidak ditemukan sinyal BARU (Semua sinyal aktif sudah dikirim sebelumnya).")

# Main Execution
if __name__ == "__main__":
    bot = CryptoScanner()
    # Anda bisa menggunakan loop while True disini jika ingin bot berjalan terus menerus
    # while True:
    bot.run_scan()
    #    time.sleep(300) # Tunggu 5 menit sebelum scan lagi
