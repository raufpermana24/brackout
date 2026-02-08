import ccxt
import pandas as pd
import pandas_ta as ta
import numpy as np
import time
import requests
import matplotlib
# Mengatur backend ke 'Agg' agar tidak error saat dijalankan di server/VPS tanpa monitor
matplotlib.use('Agg') 
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
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
TIMEFRAME = '15m'       # Timeframe Scalping
LIMIT_CANDLES = 100     # Jumlah candle analisis
TOP_COINS_COUNT = 300   # Jumlah koin top volume
VOLUME_THRESHOLD = 2.0  # Sensitivitas Volume Spike

# Konfigurasi File
HISTORY_FILE = 'scan_history.json'
SCREENSHOT_FOLDER = 'scan_results' # Folder penyimpanan gambar

class CryptoScanner:
    def __init__(self):
        print("🤖 Menginisialisasi Bot...")
        
        # 1. Buat folder screenshot jika belum ada
        if not os.path.exists(SCREENSHOT_FOLDER):
            os.makedirs(SCREENSHOT_FOLDER)
            print(f"📁 Folder '{SCREENSHOT_FOLDER}' berhasil dibuat.")

        # 2. Cek Koneksi Telegram Dulu
        self.check_telegram_connection()

        # 3. Koneksi Binance
        try:
            self.exchange = ccxt.binance({
                'apiKey': API_KEY,
                'secret': API_SECRET,
                'options': {'defaultType': 'future'},
                'enableRateLimit': True
            })
            print("✅ Terhubung ke Binance Futures")
        except Exception as e:
            print(f"❌ Gagal terhubung ke Binance: {e}")
            exit()

    def check_telegram_connection(self):
        """Mencoba mengirim pesan halo saat bot nyala"""
        print("📨 Mengetes koneksi Telegram...")
        if 'ISI_TOKEN' in TELEGRAM_TOKEN or 'ISI_CHAT_ID' in TELEGRAM_CHAT_ID:
            print("⚠️ PERINGATAN: Token Telegram belum diisi! Gambar TIDAK akan terkirim.")
            return

        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        try:
            data = {'chat_id': TELEGRAM_CHAT_ID, 'text': "✅ **BOT ONLINE**\nSistem siap. Gambar akan disimpan di folder 'scan_results'."}
            requests.post(url, data=data)
        except Exception as e:
            print(f"❌ Gagal menghubungi Telegram: {e}")

    def get_top_volume_pairs(self, limit=TOP_COINS_COUNT):
        print(f"🔄 Mengambil {limit} koin volume tertinggi...")
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
        # 1. Donchian Channels
        df['DC_Upper'] = df['high'].rolling(window=20).max()
        df['DC_Lower'] = df['low'].rolling(window=20).min()
        
        # 2. Volume Z-Score
        vol_mean = df['volume'].rolling(window=20).mean()
        vol_std = df['volume'].rolling(window=20).std()
        df['Vol_ZScore'] = (df['volume'] - vol_mean) / vol_std
        df['Vol_Threshold'] = vol_mean + (vol_std * VOLUME_THRESHOLD)
        
        # 3. Money Flow
        df['MFM'] = ((df['close'] - df['low']) - (df['high'] - df['close'])) / (df['high'] - df['low'])
        df['Net_Volume_Flow'] = df['MFM'] * df['volume']
        
        # 4. BBMA
        bb = ta.bbands(df['close'], length=20, std=2)
        if bb is not None:
            df = pd.concat([df, bb], axis=1)
        else:
            df['BBU_20_2.0'] = df['DC_Upper']
            df['BBL_20_2.0'] = df['DC_Lower']
            df['BBM_20_2.0'] = df['close'].rolling(20).mean()

        return df

    def analyze_pair(self, symbol):
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
            reasons = [] # List untuk menampung alasan teknikal
            
            # --- LOGIKA SIGNAL & ALASAN ---
            
            # 1. Cek Volume Spike
            if last_row['Vol_ZScore'] > VOLUME_THRESHOLD:
                reasons.append(f"✅ Volume Spike (Z-Score {last_row['Vol_ZScore']:.1f} > {VOLUME_THRESHOLD})")
            else:
                return None # Jika tidak ada volume, skip langsung

            # 2. Cek Arah Breakout
            price_breakout_up = last_row['close'] >= prev_row['DC_Upper'] or last_row['high'] > prev_row['DC_Upper']
            price_breakout_down = last_row['close'] <= prev_row['DC_Lower'] or last_row['low'] < prev_row['DC_Lower']
            
            strong_buying_pressure = last_row['Net_Volume_Flow'] > 0
            
            if price_breakout_up and strong_buying_pressure:
                signal = "LONG 🟢"
                reasons.append("✅ Breakout Resistance (Donchian Upper)")
                reasons.append("✅ Strong Buying Pressure (Inflow)")
            
            elif price_breakout_down and not strong_buying_pressure:
                signal = "SHORT 🔴"
                reasons.append("✅ Breakdown Support (Donchian Lower)")
                reasons.append("✅ Strong Selling Pressure (Outflow)")
                
            if signal:
                candle_time_str = str(last_row['timestamp'])
                reason_text = "\n".join(reasons)
                
                return {
                    'Pair': symbol.replace('/USDT', ''), 
                    'Price': last_row['close'],
                    'Signal': signal,
                    'Reason': reason_text, # Kirim alasan ke fungsi utama
                    'TimeID': candle_time_str,
                    'DataFrame': df
                }
        except Exception:
            return None
        return None

    def generate_chart_image(self, df, symbol, signal):
        try:
            plot_df = df.tail(60).copy()
            
            fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 8), gridspec_kw={'height_ratios': [3, 1]})
            last_price = plot_df['close'].iloc[-1]
            fig.suptitle(f"{symbol} - {signal} ({TIMEFRAME}) | P: {last_price}", fontsize=14, weight='bold')
            
            # --- Chart Harga ---
            ax1.plot(plot_df['timestamp'], plot_df['close'], color='black', linewidth=1.2, label='Price')
            if 'BBU_20_2.0' in plot_df.columns:
                ax1.fill_between(plot_df['timestamp'], plot_df['BBU_20_2.0'], plot_df['BBL_20_2.0'], color='blue', alpha=0.08)
                ax1.plot(plot_df['timestamp'], plot_df['BBU_20_2.0'], color='green', linewidth=0.8, linestyle='--')
                ax1.plot(plot_df['timestamp'], plot_df['BBL_20_2.0'], color='red', linewidth=0.8, linestyle='--')
            ax1.set_ylabel('Price')
            ax1.grid(True, alpha=0.2)
            
            # --- Chart Volume ---
            colors = ['#28a745' if c >= o else '#dc3545' for c, o in zip(plot_df['close'], plot_df['open'])]
            ax2.bar(plot_df['timestamp'], plot_df['volume'], color=colors, width=0.005, alpha=0.7)
            ax2.plot(plot_df['timestamp'], plot_df['Vol_Threshold'], color='orange', linestyle='--', linewidth=1, label='Anomaly Limit')
            ax2.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
            plt.xticks(rotation=30)
            
            # SIMPAN KE FOLDER
            timestamp_file = int(time.time())
            safe_signal = signal.replace(' ', '_').replace('🟢', 'LONG').replace('🔴', 'SHORT')
            filename = f"{SCREENSHOT_FOLDER}/{symbol}_{safe_signal}_{timestamp_file}.png"
            
            plt.tight_layout()
            plt.savefig(filename)
            plt.close(fig)
            return filename

        except Exception as e:
            print(f"❌ Error membuat gambar {symbol}: {e}")
            return None

    def load_processed_signals(self):
        if os.path.exists(HISTORY_FILE):
            try:
                with open(HISTORY_FILE, 'r') as f:
                    return json.load(f)
            except:
                return {}
        return {}

    def save_processed_signals(self, history):
        with open(HISTORY_FILE, 'w') as f:
            json.dump(history, f)

    def send_telegram_alert(self, message, image_path=None):
        if 'ISI_TOKEN' in TELEGRAM_TOKEN:
            return 

        url_msg = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        url_photo = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
        
        try:
            if image_path and os.path.exists(image_path):
                with open(image_path, 'rb') as photo:
                    requests.post(url_photo, data={'chat_id': TELEGRAM_CHAT_ID, 'caption': message}, files={'photo': photo})
            else:
                requests.post(url_msg, data={'chat_id': TELEGRAM_CHAT_ID, 'text': message})
            print(f"✅ Pesan Telegram terkirim!")
        except Exception as e:
            print(f"❌ Gagal kirim Telegram: {e}")

    def run_scan(self):
        print(f"\n🚀 SCAN START: {TOP_COINS_COUNT} Coins | {TIMEFRAME}")
        history = self.load_processed_signals()
        pairs = self.get_top_volume_pairs()
        raw_results = []
        
        with ThreadPoolExecutor(max_workers=20) as executor:
            futures = [executor.submit(self.analyze_pair, pair) for pair in pairs]
            for i, future in enumerate(futures):
                res = future.result()
                if res:
                    raw_results.append(res)
                if i % 50 == 0:
                    print(f"⏳ Progress: {i}/{len(pairs)}...")
        
        print("\n🔍 Analisis Sinyal & Kirim Alert...")
        found_new = False
        
        for res in raw_results:
            unique_id = f"{res['Pair']}_{res['Signal']}_{res['TimeID']}"
            
            if unique_id not in history:
                found_new = True
                print(f"🔔 NEW SIGNAL: {res['Pair']} {res['Signal']}")
                
                # Buat Gambar & Simpan
                chart_file = self.generate_chart_image(res['DataFrame'], res['Pair'], res['Signal'])
                
                # Buat Pesan Lengkap dengan ALASAN
                msg = (f"🚨 **CRYPTO ALERT {TIMEFRAME}** 🚨\n\n"
                       f"💎 **#{res['Pair']}**\n"
                       f"Sinyal: **{res['Signal']}**\n"
                       f"Harga: {res['Price']}\n\n"
                       f"📊 **ANALISIS TEKNIKAL:**\n"
                       f"{res['Reason']}\n\n"
                       f"⚠️ _Bot AI dengan Volume Spike Detection_")
                
                self.send_telegram_alert(msg, chart_file)
                
                # Simpan ID ke history
                history[unique_id] = True
        
        self.save_processed_signals(history)
        
        if not found_new:
            print("💤 Tidak ada sinyal BARU yang valid.")
        print("="*40)

if __name__ == "__main__":
    bot = CryptoScanner()
    # Anda bisa uncomment baris bawah untuk loop otomatis
    # while True:
    bot.run_scan()
    #     print("Menunggu 5 menit...")
    #     time.sleep(300)
