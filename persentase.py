import os
import time
import requests
import threading
from binance.client import Client
from binance import BinanceSocketManager
from twisted.internet import reactor

# --- KONFIGURASI KREDENSIAL ---
BINANCE_API_KEY = os.environ.get('BINANCE_API_KEY', 'fZwDMOfBL6rDU9jfUQox64fUAb2RSN48myxMPUGDAINYjmLdqJmUFhVRWLqlsX97')
BINANCE_SECRET_KEY = os.environ.get('BINANCE_API_SECRET', 'FmZNNbIOWIAddxVoLcNowLNW379E6gxyM85Bvy3QzlRMtK1eMApJp6vJtpGHWdWB')
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', '8000712659:AAHltp77nGuakOzW9QMgQpVqnd5f1KgEsKA')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '-1003812500986')

# --- SETTING ---
TIMEFRAMES = {
    '15m': Client.KLINE_INTERVAL_15MINUTE,
    '1h': Client.KLINE_INTERVAL_1HOUR,
    '4h': Client.KLINE_INTERVAL_4HOUR
}

# Struktur Memori: { '15m': { 'BTCUSDT': [c1_change, c2_change] }, ... }
analysis_data = {tf: {} for tf in TIMEFRAMES.keys()}
client = Client(BINANCE_API_KEY, BINANCE_SECRET_KEY)

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"[!] Error Telegram: {e}")

def get_historical_data(symbol, tf_key):
    """Fungsi Fallback: Mengambil data via REST API jika WebSocket belum mencukupi."""
    try:
        interval = TIMEFRAMES[tf_key]
        # Ambil 3 candle (2 yang sudah close, 1 yang sedang berjalan)
        klines = client.futures_klines(symbol=symbol, interval=interval, limit=3)
        if len(klines) < 3: return None
        
        # Hitung candle yang sudah tertutup
        c1_open, c1_close = float(klines[0][1]), float(klines[0][4])
        c2_open, c2_close = float(klines[1][1]), float(klines[1][4])
        
        c1_pct = ((c1_close - c1_open) / c1_open) * 100
        c2_pct = ((c2_close - c2_open) / c2_open) * 100
        
        return c1_pct, c2_pct, float(klines[1][4]) # Return data tertutup terakhir
    except Exception as e:
        return None

def check_signal(symbol, tf_key, c1_pct, c2_pct, price):
    """Logika utama: C1 > 1% dan C2 > 2%"""
    if c1_pct > 1.0 and c2_pct > 2.0:
        print(f"🚀 SIGNAL [{tf_key}] {symbol}: {c1_pct:.2f}% -> {c2_pct:.2f}%")
        msg = (
            f"🚀 *FUTURES SIGNAL: {symbol}*\n\n"
            f"⏱️ *Timeframe:* {tf_key}\n"
            f"1️⃣ *Candle 1:* `+{c1_pct:.2f}%` (Closed)\n"
            f"2️⃣ *Candle 2:* `+{c2_pct:.2f}%` (Closed)\n"
            f"💰 *Price:* `{price}`\n\n"
            f"🔗 [Binance Chart](https://www.binance.com/en/futures/{symbol})"
        )
        send_telegram(msg)

def socket_callback(msg):
    """Handler data dari WebSocket."""
    try:
        if 'data' not in msg: return
        d = msg['data']
        symbol = d['s']
        candle = d['k']
        tf_code = candle['i'] # misal '15m', '1h'
        is_closed = candle['x']
        
        # Cari tf_key dari value
        tf_key = next((k for k, v in TIMEFRAMES.items() if v == tf_code), None)
        if not tf_key: return

        if is_closed:
            open_p = float(candle['o'])
            close_p = float(candle['c'])
            current_pct = ((close_p - open_p) / open_p) * 100
            
            # Jika data di memori kosong, gunakan REST API Fallback
            if symbol not in analysis_data[tf_key]:
                print(f"[*] Initializing {symbol} {tf_key} via REST API...")
                hist = get_historical_data(symbol, tf_key)
                if hist:
                    c1, c2, last_p = hist
                    analysis_data[tf_key][symbol] = c2 # Simpan candle terakhir yang close
                    check_signal(symbol, tf_key, c1, c2, last_p)
            else:
                # Jika sudah ada di memori, bandingkan data baru dengan data sebelumnya
                prev_pct = analysis_data[tf_key][symbol]
                check_signal(symbol, tf_key, prev_pct, current_pct, close_p)
                # Update memori
                analysis_data[tf_key][symbol] = current_pct
    except Exception as e:
        pass

# --- FUNGSI TAMBAHAN: SCAN HISTORICAL 2 HARI ---
def scan_historical_signals_2_days(symbols):
    """Mencari sinyal dari 2 hari ke belakang dan mengirimkannya."""
    print("[*] Memulai pemindaian data historis 2 hari ke belakang...")
    
    # Limit candle untuk merepresentasikan 2 hari (48 jam)
    limit_map = {
        '15m': 48 * 4,  # 192 candle
        '1h': 48,       # 48 candle
        '4h': 12        # 12 candle
    }
    
    for symbol in symbols:
        for tf_key, interval in TIMEFRAMES.items():
            try:
                limit = limit_map[tf_key]
                # Mengambil data historis sesuai limit timeframe
                klines = client.futures_klines(symbol=symbol, interval=interval, limit=limit)
                
                if len(klines) < 3:
                    continue
                    
                # Iterasi dari candle tertua ke terbaru
                for i in range(1, len(klines) - 1): # Abaikan index 0 (tidak punya prev) dan index terakhir (sedang berjalan)
                    c1_open = float(klines[i-1][1])
                    c1_close = float(klines[i-1][4])
                    c1_pct = ((c1_close - c1_open) / c1_open) * 100
                    
                    c2_open = float(klines[i][1])
                    c2_close = float(klines[i][4])
                    c2_pct = ((c2_close - c2_open) / c2_open) * 100
                    
                    if c1_pct > 1.0 and c2_pct > 2.0:
                        # Konversi waktu UNIX dari Binance ke format yang bisa dibaca
                        close_time = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(klines[i][6] / 1000))
                        price = c2_close
                        
                        print(f"🕰️ HISTORICAL SIGNAL [{tf_key}] {symbol} at {close_time}")
                        msg = (
                            f"🕰️ *HISTORICAL SIGNAL (Past 2 Days)*\n\n"
                            f"💎 *Symbol:* #{symbol}\n"
                            f"⏱️ *Timeframe:* {tf_key}\n"
                            f"📅 *Waktu Close:* {close_time}\n"
                            f"1️⃣ *Candle 1:* `+{c1_pct:.2f}%`\n"
                            f"2️⃣ *Candle 2:* `+{c2_pct:.2f}%`\n"
                            f"💰 *Price (Then):* `{price}`\n\n"
                            f"🔗 [Binance Chart](https://www.binance.com/en/futures/{symbol})"
                        )
                        send_telegram(msg)
                        time.sleep(0.2) # Jeda agar tidak terkena limit spam dari Telegram
                        
                time.sleep(0.05) # Jeda ringan untuk menjaga batas rate limit API Binance
            except Exception as e:
                pass # Lewati jika ada error pada koin tertentu
                
    print("[*] ✅ Pemindaian data historis 2 hari telah selesai.")

def run_scanner():
    print("=== BINANCE HYBRID MULTI-TF SCANNER STARTED ===")
    print(f"Monitoring: {list(TIMEFRAMES.keys())}")
    
    # Ambil semua simbol futures aktif
    try:
        info = client.futures_exchange_info()
        symbols = [s['symbol'] for s in info['symbols'] if s['quoteAsset'] == 'USDT' and s['status'] == 'TRADING']
        print(f"[*] Scanning {len(symbols)} market koin...")
    except Exception as e:
        print(f"[!] Gagal ambil daftar simbol: {e}")
        return

    # --- TAMBAHAN: Jalankan scan riwayat di thread terpisah ---
    threading.Thread(target=scan_historical_signals_2_days, args=(symbols,), daemon=True).start()

    bsm = BinanceSocketManager(client)
    
    # Daftarkan semua stream (Multi-TF untuk semua koin)
    streams = []
    for s in symbols:
        for tf in TIMEFRAMES.values():
            streams.append(f"{s.lower()}@kline_{tf}")
    
    # Mulai koneksi WebSocket Multiplex
    # Catatan: Binance mengizinkan banyak stream dalam satu koneksi
    # Kita pecah jadi beberapa koneksi jika terlalu banyak (otomatis ditangani library)
    bsm.start_multiplex_socket(streams, socket_callback)
    bsm.start()

if __name__ == "__main__":
    try:
        run_scanner()
        # Menjaga script tetap hidup
        while True:
            time.sleep(10)
    except KeyboardInterrupt:
        print("\n[!] Bot stopped.")
