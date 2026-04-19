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
matplotlib.use('Agg') # Render chart di background tanpa GUI
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
        
        # Lock untuk mengamankan data dari tabrakan antar Thread (Thread Safety)
        self.data_lock = threading.Lock()
        
        # Inisialisasi CCXT untuk persiapan fitur trading/fetching advance kedepannya
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
            with self.data_lock: # Kunci memori saat update data
                for koin in data:
                    simbol = koin['s']
                    if simbol.endswith('USDT') and '_' not in simbol:
                        self.market_data[simbol] = {
                            'simbol': simbol,
                            'harga_sekarang': float(koin['c']),
                            'volume_koin': float(koin['v']),
                            'volume_usdt_asli': float(koin['q']),
                        }
        except Exception:
            pass

    def on_error(self, ws, error):
        print(f"[-] WS Error: {error}")

    def on_close(self, ws, close_status_code, close_msg):
        self.ws_connected = False
        print("[!] WS Terputus. Mencoba reconect otomatis di background...")

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
    # REST API FALLBACK (CCXT / REQUESTS)
    # ==========================================
    def ambil_data_rest_api(self):
        """Fallback menggunakan REST API standar jika WS putus"""
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
                            'volume_usdt_asli': float(koin['quoteVolume'])
                        }
        except Exception as e:
            print(f"[-] Gagal REST API:")
            traceback.print_exc()

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
    # PANDAS ENGINE: PEMROSESAN DATA SUPER CEPAT
    # ==========================================
    def proses_dan_kirim(self):
        if len(self.market_data) < 150:
            self.ambil_data_rest_api()

        if not self.market_data:
            return

        # 1. Ambil data dengan aman menggunakan copy untuk menghindari Error Iteration
        with self.data_lock:
            data_mentah = copy.deepcopy(self.market_data)

        # 2. Konversi ke Pandas DataFrame (Sangat Cepat & Efisien)
        df = pd.DataFrame.from_dict(data_mentah, orient='index')
        
        # 3. Filter data (Hapus volume 0 untuk hindari error bagi nol / infinity)
        df = df[df['volume_koin'] > 0]
        
        # 4. Kalkulasi Vectorized dengan Pandas & Numpy (Selesai dalam hitungan milidetik)
        df['kalkulasi_usdt'] = df['volume_koin'] * df['harga_sekarang']
        df['selisih_usdt'] = df['kalkulasi_usdt'] - df['volume_usdt_asli']
        df['harga_target'] = df['volume_usdt_asli'] / df['volume_koin']

        # Waktu Pemrosesan
        waktu_update = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        batas = 50

        # --- LAPORAN 1: TOP 50 VOLUME USDT ASLI ---
        df_vol_usdt = df.nlargest(batas, 'volume_usdt_asli')
        pesan_vol_usdt = f"💰 <b>TOP {batas} VOL USDT (ASLI)</b> 💰\n"
        pesan_vol_usdt += f"🕒 <i>{waktu_update}</i>\n<code>Koin | Harga | Vol USDT</code>\n----------------------------\n"
        
        for i, row in enumerate(df_vol_usdt.itertuples(), 1):
            nama = row.simbol.replace("USDT", "")
            pesan_vol_usdt += f"{i}. <b>{nama}</b> | {format_harga(row.harga_sekarang)} | <b>${format_angka_besar(row.volume_usdt_asli)}</b>\n"
        
        self.kirim_pesan_telegram(pesan_vol_usdt)
        time.sleep(1)

        # --- LAPORAN 2: TOP 50 VOLUME KOIN ---
        df_vol_koin = df.nlargest(batas, 'volume_koin')
        pesan_vol_koin = f"🪙 <b>TOP {batas} VOL KOIN</b> 🪙\n"
        pesan_vol_koin += f"🕒 <i>{waktu_update}</i>\n<code>Koin | Jml Token | Kalk USDT</code>\n----------------------------\n"
        
        for i, row in enumerate(df_vol_koin.itertuples(), 1):
            nama = row.simbol.replace("USDT", "")
            pesan_vol_koin += f"{i}. <b>{nama}</b> | {format_angka_besar(row.volume_koin)} | <b>${format_angka_besar(row.kalkulasi_usdt)}</b>\n"
            
        self.kirim_pesan_telegram(pesan_vol_koin)
        time.sleep(1)

        # --- LAPORAN 3 & 4: ANALISA VWAP (SELISIH) ---
        df_selisih = df.nlargest(batas, 'selisih_usdt')
        
        # Part 1 (Top 1-25)
        pesan_analisa_1 = f"🎯 <b>ANALISA VWAP (Top 1-25)</b> 🎯\n"
        pesan_analisa_1 += f"🕒 <i>{waktu_update}</i>\n<code>Koin | Harga Skrg -> Harga Tgt | Selisih</code>\n---------------------------------------\n"
        
        for i, row in enumerate(df_selisih.head(25).itertuples(), 1):
            nama = row.simbol.replace("USDT", "")
            indikator = "🟢" if row.harga_sekarang >= row.harga_target else "🔴"
            pesan_analisa_1 += f"{i}. <b>{nama}</b>\n↳ {indikator} {format_harga(row.harga_sekarang)} -> 🎯 {format_harga(row.harga_target)} | <b>${format_angka_besar(row.selisih_usdt)}</b>\n"
            
        self.kirim_pesan_telegram(pesan_analisa_1)
        time.sleep(1)

        # Part 2 (Top 26-50)
        pesan_analisa_2 = f"🎯 <b>ANALISA VWAP (Top 26-50)</b> 🎯\n---------------------------------------\n"
        
        # Ambil baris ke 25 sampai akhir (50) menggunakan Pandas iloc
        for i, row in enumerate(df_selisih.iloc[25:].itertuples(), 26):
            nama = row.simbol.replace("USDT", "")
            indikator = "🟢" if row.harga_sekarang >= row.harga_target else "🔴"
            pesan_analisa_2 += f"{i}. <b>{nama}</b>\n↳ {indikator} {format_harga(row.harga_sekarang)} -> 🎯 {format_harga(row.harga_target)} | <b>${format_angka_besar(row.selisih_usdt)}</b>\n"
            
        pesan_analisa_2 += "\n🤖 <i>Scanner Engine: Pandas + CCXT</i>"
        self.kirim_pesan_telegram(pesan_analisa_2)

        os.system('cls' if os.name == 'nt' else 'clear')
        print(f"=== Selesai Memproses Laporan Pandas DataFrame ({waktu_update}) ===")

    def jalankan(self, interval_menit=5):
        self.mulai_websocket()
        print("Menunggu 5 detik untuk mengumpulkan data stream Binance...")
        time.sleep(5)

        try:
            while True:
                # Menggunakan ThreadPoolExecutor untuk pemrosesan paralel jika dibutuhkan kedepannya
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
