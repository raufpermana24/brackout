import ccxt
import pandas as pd
import pandas_ta as ta
import numpy as np
import time
import requests
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
TIMEFRAME = '1h'       # Timeframe (15m, 1h, 4h)
LIMIT_CANDLES = 100    # Jumlah candle yang dianalisis
TOP_COINS_COUNT = 300  # Jumlah koin yang di-scan
VOLUME_THRESHOLD = 2.0 # Z-Score Volume (Batas Anomaly)

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
        Inti dari Logika 'AI' dan Algoritma.
        """
        # 1. Donchian Channels (Breakout)
        df['DC_Upper'] = df['high'].rolling(window=20).max()
        df['DC_Lower'] = df['low'].rolling(window=20).min()
        
        # 2. Volume Analysis (AI Anomaly)
        # Menghitung Rata-rata dan Standar Deviasi Volume 20 periode
        vol_mean = df['volume'].rolling(window=20).mean()
        vol_std = df['volume'].rolling(window=20).std()
        
        # Z-Score: Seberapa jauh volume saat ini menyimpang dari rata-rata
        df['Vol_ZScore'] = (df['volume'] - vol_mean) / vol_std
        
        # Simpan batas threshold volume untuk visualisasi di chart
        df['Vol_Threshold'] = vol_mean + (vol_std * VOLUME_THRESHOLD)
        
        # 3. Money Flow
        df['MFM'] = ((df['close'] - df['low']) - (df['high'] - df['close'])) / (df['high'] - df['low'])
        df['Net_Volume_Flow'] = df['MFM'] * df['volume']
        
        # 4. Trend Filter (EMA 200)
        df['EMA_200'] = ta.ema(df['close'], length=200)
        
        # 5. Bollinger Bands (BBMA)
        # length=20, std=2.0 (Standar BB)
        bb = ta.bbands(df['close'], length=20, std=2)
        # Gabungkan hasil BB ke DataFrame utama
        # Kolom output pandas_ta: BBL_20_2.0 (Lower), BBM_20_2.0 (Mid), BBU_20_2.0 (Upper)
        df = pd.concat([df, bb], axis=1)

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
            # 1. Volume Anomaly (Spike)
            volume_breakout = last_row['Vol_ZScore'] > VOLUME_THRESHOLD
            
            # 2. Price Breakout (Donchian Channel)
            price_breakout_up = last_row['close'] >= prev_row['DC_Upper'] or last_row['high'] > prev_row['DC_Upper']
            price_breakout_down = last_row['close'] <= prev_row['DC_Lower'] or last_row['low'] < prev_row['DC_Lower']
            
            # 3. Buying/Selling Pressure
            strong_buying_pressure = last_row['Net_Volume_Flow'] > 0
            
            # --- PENENTUAN POSISI ---
            if price_breakout_up and volume_breakout and strong_buying_pressure:
                signal = "LONG 🟢" 
                strength = f"Z-Score: {last_row['Vol_ZScore']:.2f}"
            
            elif price_breakout_down and volume_breakout and not strong_buying_pressure:
                signal = "SHORT 🔴"
                strength = f"Z-Score: {last_row['Vol_ZScore']:.2f}"
                
            if signal:
                candle_time_str = str(last_row['timestamp'])
                return {
                    'Pair': symbol.replace('/USDT', ''), 
                    'Price': last_row['close'],
                    'Signal': signal,
                    'Vol': strength,
                    'TimeID': candle_time_str,
                    'DataFrame': df  # PENTING: Mengirim DF untuk digambar
                }
                
        except Exception:
            return None
        return None

    def generate_chart_image(self, df, symbol, signal):
        """
        Membuat Chart Visual yang informatif:
        1. Harga + BBMA
        2. Volume + Threshold Anomaly
        """
        try:
            # Ambil 50 candle terakhir agar chart jelas
            plot_df = df.tail(50).copy()
            
            # Setup Plot (2 Baris: Harga & Volume)
            fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 10), gridspec_kw={'height_ratios': [3, 1]})
            
            # Judul Chart
            last_price = plot_df['close'].iloc[-1]
            fig.suptitle(f"{symbol} - {signal} ({TIMEFRAME}) | Price: {last_price}", fontsize=16, weight='bold')
            
            # --- SUBPLOT 1: HARGA & BBMA ---
            # Plot Harga Close (Garis Hitam)
            ax1.plot(plot_df['timestamp'], plot_df['close'], label='Price', color='black', linewidth=1.5, zorder=3)
            
            # Plot Bollinger Bands (BBMA)
            # Area BB diarsir abu-abu
            ax1.fill_between(plot_df['timestamp'], plot_df['BBU_20_2.0'], plot_df['BBL_20_2.0'], color='blue', alpha=0.1)
            ax1.plot(plot_df['timestamp'], plot_df['BBU_20_2.0'], color='green', linewidth=1, linestyle='--', alpha=0.7, label='BB Top')
            ax1.plot(plot_df['timestamp'], plot_df['BBL_20_2.0'], color='red', linewidth=1, linestyle='--', alpha=0.7, label='BB Low')
            ax1.plot(plot_df['timestamp'], plot_df['BBM_20_2.0'], color='orange', linewidth=1.2, label='BB Mid (MA20)') # Garis Tengah BB
            
            # Tanda Panah Sinyal di Candle Terakhir
            last_time = plot_df['timestamp'].iloc[-1]
            last_high = plot_df['high'].iloc[-1]
            last_low = plot_df['low'].iloc[-1]
            
            if "LONG" in signal:
                ax1.annotate('BUY SIGNAL', xy=(last_time, last_low), xytext=(last_time, last_low - (last_low*0.01)),
                             arrowprops=dict(facecolor='green', shrink=0.05),
                             horizontalalignment='center', color='green', weight='bold')
            else:
                ax1.annotate('SELL SIGNAL', xy=(last_time, last_high), xytext=(last_time, last_high + (last_high*0.01)),
                             arrowprops=dict(facecolor='red', shrink=0.05),
                             horizontalalignment='center', color='red', weight='bold')

            ax1.set_ylabel('Price (USDT)')
            ax1.legend(loc='upper left')
            ax1.grid(True, alpha=0.3)
            
            # --- SUBPLOT 2: VOLUME ANOMALY ---
            # Warna volume: Hijau jika Close > Open, Merah jika sebaliknya
            colors = ['#28a745' if c >= o else '#dc3545' for c, o in zip(plot_df['close'], plot_df['open'])]
            ax2.bar(plot_df['timestamp'], plot_df['volume'], color=colors, width=0.02, alpha=0.8, label='Volume')
            
            # Plot Garis Threshold Anomaly (Garis Oranye Putus-putus)
            # Ini menunjukkan batas normal. Jika bar volume menembus garis ini -> ANOMALY
            ax2.plot(plot_df['timestamp'], plot_df['Vol_Threshold'], color='orange', linestyle='--', linewidth=1.5, label='Anomaly Limit (Z=2.0)')
            
            # Highlight Candle Terakhir (Sinyal)
            ax2.scatter(last_time, plot_df['volume'].iloc[-1], color='blue', s=100, zorder=5, label='Signal Vol')

            ax2.set_ylabel('Volume')
            ax2.legend(loc='upper left')
            ax2.grid(True, alpha=0.3)

            # Format Tanggal di Sumbu X
            ax2.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
            plt.xticks(rotation=45)
            
            # Simpan Gambar
            filename = f"chart_{symbol}_{int(time.time())}.png"
            plt.tight_layout()
            plt.savefig(filename)
            plt.close()
            return filename

        except Exception as e:
            print(f"❌ Gagal membuat chart untuk {symbol}: {e}")
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
        if TELEGRAM_TOKEN == 'ISI_TOKEN_BOT_TELEGRAM_DISINI':
            print(f"⚠️ Mode Demo (No Telegram Token): {message}")
            return

        url_msg = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        url_photo = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
        
        try:
            if image_path:
                with open(image_path, 'rb') as photo:
                    requests.post(url_photo, data={'chat_id': TELEGRAM_CHAT_ID, 'caption': message}, files={'photo': photo})
            else:
                requests.post(url_msg, data={'chat_id': TELEGRAM_CHAT_ID, 'text': message})
            print(f"✅ Notifikasi Telegram terkirim!")
        except Exception as e:
            print(f"❌ Gagal kirim Telegram: {e}")

    def run_scan(self):
        print(f"\n🚀 Memulai Scan Cepat pada {TOP_COINS_COUNT} koin. Timeframe: {TIMEFRAME}")
        
        history = self.load_processed_signals()
        pairs = self.get_top_volume_pairs()
        raw_results = []
        
        with ThreadPoolExecutor(max_workers=20) as executor:
            futures = [executor.submit(self.analyze_pair, pair) for pair in pairs]
            
            total = len(pairs)
            for i, future in enumerate(futures):
                res = future.result()
                if res:
                    raw_results.append(res)
                if i % 50 == 0:
                    print(f"⏳ Progress: {i}/{total}...")
        
        print("\n🔍 Memeriksa duplikasi sinyal...")
        new_signals_found = False
        
        for res in raw_results:
            unique_id = f"{res['Pair']}_{res['Signal']}_{res['TimeID']}"
            
            if unique_id not in history:
                new_signals_found = True
                print(f"🔔 Sinyal Baru: {res['Pair']} - {res['Signal']}")
                
                history[unique_id] = True
                
                # Buat Chart Visual
                chart_file = self.generate_chart_image(res['DataFrame'], res['Pair'], res['Signal'])
                
                msg = (f"🚨 **CRYPTO SIGNAL ALERT** 🚨\n"
                       f"Asset: #{res['Pair']}\n"
                       f"Signal: {res['Signal']}\n"
                       f"Price: {res['Price']}\n"
                       f"📊 **AI Analysis:**\n"
                       f"• {res['Vol']} (Spike Detected!)\n"
                       f"• Donchian Channel Breakout\n"
                       f"• BBMA Trend Confirmation")
                
                self.send_telegram_alert(msg, chart_file)
                
                if chart_file and os.path.exists(chart_file):
                    os.remove(chart_file)
            else:
                pass

        self.save_processed_signals(history)

        print("\n" + "="*60)
        if not new_signals_found:
            print("✅ Scan selesai. Tidak ada sinyal BARU saat ini.")
        else:
            print("✅ Scan selesai. Sinyal baru telah dikirim ke Telegram.")
        print("="*60)

# Main Execution
if __name__ == "__main__":
    bot = CryptoScanner()
    bot.run_scan()
