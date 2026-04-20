import ccxt
import pandas as pd
import numpy as np
import time
from datetime import datetime
import os
import requests
import concurrent.futures
import threading
import json
import websocket
import copy
import traceback

import matplotlib
matplotlib.use('Agg') 
import matplotlib.pyplot as plt
import io

# --- KONFIGURASI API & TELEGRAM ---
BINANCE_API_KEY = os.environ.get('BINANCE_API_KEY', 'fZwDMOfBL6rDU9jfUQox64fUAb2RSN48myxMPUGDAINYjmLdqJmUFhVRWLqlsX97')
BINANCE_SECRET_KEY = os.environ.get('BINANCE_API_SECRET', 'FmZNNbIOWIAddxVoLcNowLNW379E6gxyM85Bvy3QzlRMtK1eMApJp6vJtpGHWdWB')
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', '8562793193:AAHDulfzVhhnuPfNfy4Zk6ONBNSNbGwVJ8c')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '-1003835878828')

def format_angka_besar(angka):
    """Fungsi hemat karakter untuk Telegram"""
    if pd.isna(angka): return "0"
    is_negatif = angka < 0
    angka = abs(angka)
    
    if angka >= 1_000_000_000_000:
        hasil = f"{angka/1_000_000_000_000:.2f}T"
    elif angka >= 1_000_000_000:
        hasil = f"{angka/1_000_000_000:.2f}B"
    elif angka >= 1_000_000:
        hasil = f"{angka/1_000_000:.2f}M"
    elif angka >= 1_000:
        hasil = f"{angka/1_000:.2f}K"
    else:
        hasil = f"{angka:.2f}"
        
    return f"-{hasil}" if is_negatif else hasil

def format_harga(harga):
    """Format harga dinamis"""
    if pd.isna(harga): return "$0"
    if harga < 1:
        return f"${harga:,.6f}".rstrip('0').rstrip('.')
    else:
        return f"${harga:,.2f}"

class BinanceProScanner:
    def __init__(self):
        self.ws_url = "wss://fstream.binance.com/ws/!ticker@arr"
        self.rest_url = "https://fapi.binance.com/fapi/v1/ticker/24hr"
        self.market_data = {}
        self.ws_connected = False
        self.data_lock = threading.Lock()
        
        self.exchange = ccxt.binanceusdm({
            'apiKey': BINANCE_API_KEY,
            'secret': BINANCE_SECRET_KEY,
            'enableRateLimit': True,
        })

    # ==========================================
    # WEBSOCKET HANDLING (BACKGROUND)
    # ==========================================
    def on_message(self, ws, message):
        try:
            data = json.loads(message)
            with self.data_lock: 
                for koin in data:
                    simbol = koin['s']
                    if simbol.endswith('USDT') and '_' not in simbol:
                        self.market_data[simbol] = {
                            'simbol': simbol,
                            'harga_sekarang': float(koin['c']),
                            'volume_koin': float(koin['v']),
                            'volume_usdt_asli': float(koin['q']),
                            'perubahan_persen': float(koin['P']) # <--- TAMBAHAN: Ambil % perubahan 24j
                        }
        except Exception:
            pass

    def on_error(self, ws, error):
        print(f"[-] WS Error: {error}")

    def on_close(self, ws, close_status_code, close_msg):
        self.ws_connected = False
        print("[!] WS Terputus. Bot akan menggunakan REST API sementara.")

    def on_open(self, ws):
        self.ws_connected = True
        print("[+] WS Stream Binance Aktif.")

    def mulai_websocket(self):
        ws = websocket.WebSocketApp(self.ws_url,
                                    on_open=self.on_open,
                                    on_message=self.on_message,
                                    on_error=self.on_error,
                                    on_close=self.on_close)
        wst = threading.Thread(target=ws.run_forever)
        wst.daemon = True
        wst.start()

    # ==========================================
    # REST API FALLBACK
    # ==========================================
    def ambil_data_rest_api(self):
        headers = {'X-MBX-APIKEY': BINANCE_API_KEY}
        try:
            respons = requests.get(self.rest_url, headers=headers)
            respons.raise_for_status()
            data = respons.json()
            
            with self.data_lock:
                for koin in data:
                    simbol = koin['symbol']
                    if simbol.endswith('USDT') and '_' not in simbol:
                        self.market_data[simbol] = {
                            'simbol': simbol,
                            'harga_sekarang': float(koin['lastPrice']),
                            'volume_koin': float(koin['volume']),
                            'volume_usdt_asli': float(koin['quoteVolume']),
                            'perubahan_persen': float(koin['priceChangePercent']) # <--- TAMBAHAN REST API
                        }
        except Exception as e:
            pass

    # ==========================================
    # TELEGRAM SENDER
    # ==========================================
    def kirim_pesan_telegram(self, pesan):
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {
            'chat_id': TELEGRAM_CHAT_ID,
            'text': pesan,
            'parse_mode': 'HTML',
            'disable_web_page_preview': True
        }
        try:
            requests.post(url, data=payload, timeout=10)
        except Exception as e:
            print(f"[-] Error Telegram: {e}")

    # ==========================================
    # PANDAS ENGINE: PROSES GAINERS & LOSERS
    # ==========================================
    def proses_dan_kirim(self):
        if len(self.market_data) < 150:
            self.ambil_data_rest_api()

        if not self.market_data:
            return

        with self.data_lock:
            data_mentah = copy.deepcopy(self.market_data)

        # 1. Konversi ke DataFrame
        df = pd.DataFrame.from_dict(data_mentah, orient='index')
        df = df[df['volume_koin'] > 0]
        
        # 2. Kalkulasi Vectorized (VWAP & Selisih) untuk seluruh data agar cepat
        df['kalkulasi_usdt'] = df['volume_koin'] * df['harga_sekarang']
        df['selisih_usdt'] = df['kalkulasi_usdt'] - df['volume_usdt_asli']
        df['harga_target'] = df['volume_usdt_asli'] / df['volume_koin']

        # 3. FILTERING: Ambil 50 Top Gainers dan 50 Top Losers
        top_gainers = df.nlargest(50, 'perubahan_persen') # 50 Teratas (Paling Plus)
        top_losers = df.nsmallest(50, 'perubahan_persen') # 50 Terbawah (Paling Minus)

        waktu_update = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # ================================================================
        # FUNGSI HELPER UNTUK MEMBUAT TEKS LAPORAN
        # ================================================================
        def buat_laporan(judul, dataframe, ikon, start_idx=1):
            pesan = f"{ikon} <b>{judul}</b> {ikon}\n"
            pesan += f"🕒 <i>{waktu_update}</i>\n"
            pesan += "<code>Koin (%24j) | Skrg -> Tgt | Selisih</code>\n"
            pesan += "-" * 38 + "\n"
            
            for i, row in enumerate(dataframe.itertuples(), start_idx):
                nama = row.simbol.replace("USDT", "")
                persen = f"+{row.perubahan_persen:.2f}%" if row.perubahan_persen > 0 else f"{row.perubahan_persen:.2f}%"
                
                indikator_vwap = "🟢" if row.harga_sekarang >= row.harga_target else "🔴"
                
                pesan += f"{i}. <b>{nama}</b> ({persen})\n"
                pesan += f"↳ {indikator_vwap} {format_harga(row.harga_sekarang)} -> 🎯 {format_harga(row.harga_target)} | <b>${format_angka_besar(row.selisih_usdt)}</b>\n"
            
            return pesan

        # --- KIRIM LAPORAN TOP GAINERS (Dibagi 2 karena limit Telegram) ---
        pesan_gainer_1 = buat_laporan("TOP GAINERS 1-25", top_gainers.iloc[0:25], "📈")
        self.kirim_pesan_telegram(pesan_gainer_1)
        time.sleep(1.5)

        pesan_gainer_2 = buat_laporan("TOP GAINERS 26-50", top_gainers.iloc[25:50], "📈", start_idx=26)
        self.kirim_pesan_telegram(pesan_gainer_2)
        time.sleep(1.5)

        # --- KIRIM LAPORAN TOP LOSERS (Dibagi 2 karena limit Telegram) ---
        pesan_loser_1 = buat_laporan("TOP LOSERS 1-25", top_losers.iloc[0:25], "📉")
        self.kirim_pesan_telegram(pesan_loser_1)
        time.sleep(1.5)

        pesan_loser_2 = buat_laporan("TOP LOSERS 26-50", top_losers.iloc[25:50], "📉", start_idx=26)
        pesan_loser_2 += "\n🤖 <i>Scanner Engine: Pandas + WS</i>"
        self.kirim_pesan_telegram(pesan_loser_2)

        os.system('cls' if os.name == 'nt' else 'clear')
        print(f"=== Selesai Mengirim Laporan Gainers & Losers ({waktu_update}) ===")

    def jalankan(self, interval_menit=5):
        self.mulai_websocket()
        print("Menunggu 5 detik untuk mengumpulkan data stream Binance...")
        time.sleep(5)

        try:
            while True:
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                    executor.submit(self.proses_dan_kirim)
                
                waktu_tunggu = interval_menit * 60
                print(f"Menunggu {interval_menit} menit untuk siklus berikutnya...\n")
                time.sleep(waktu_tunggu)
                
        except KeyboardInterrupt:
            print("\n[!] Bot Scanner Pandas dihentikan.")
        except Exception as e:
            traceback.print_exc()

if __name__ == "__main__":
    scanner = BinanceProScanner()
    scanner.jalankan(interval_menit=5)
