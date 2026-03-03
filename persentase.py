import os
import time
import requests
import threading
from binance.client import Client
from binance import ThreadedWebsocketManager

# --- KONFIGURASI KREDENSIAL ---
BINANCE_API_KEY = os.environ.get('BINANCE_API_KEY', 'fZwDMOfBL6rDU9jfUQox64fUAb2RSN48myxMPUGDAINYjmLdqJmUFhVRWLqlsX97')
BINANCE_SECRET_KEY = os.environ.get('BINANCE_API_SECRET', 'FmZNNbIOWIAddxVoLcNowLNW379E6gxyM85Bvy3QzlRMtK1eMApJp6vJtpGHWdWB')
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', '8000712659:AAHltp77nGuakOzW9QMgQpVqnd5f1KgEsKA')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '-1003812500986')

# --- INISIALISASI ---
client = Client(BINANCE_API_KEY, BINANCE_SECRET_KEY)

# Struktur Memori Internal
# Format: { 'BTCUSDT': {'prev_pct': 0.0, 'signal_sent': False} }
coin_data = {}

def send_telegram(message):
    """Mengirim pesan ke Telegram."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"[!] Error Telegram: {e}")

def init_historical_data(symbol):
    """Mengambil data 1 candle terakhir (1m) agar bot bisa langsung bekerja."""
    try:
        # Ambil 2 candle terakhir (1 close, 1 running)
        klines = client.futures_klines(symbol=symbol, interval=Client.KLINE_INTERVAL_1MINUTE, limit=2)
        if len(klines) >= 2:
            c1_open, c1_close = float(klines[0][1]), float(klines[0][4])
            c1_pct = ((c1_close - c1_open) / c1_open) * 100
            
            coin_data[symbol] = {
                'prev_pct': c1_pct,
                'signal_sent': False
            }
            return True
    except Exception:
        pass
    return False

def init_all_coins(symbols):
    """Fungsi untuk inisialisasi awal semua koin menggunakan Threading agar cepat."""
    print(f"[*] Mengunduh data awal untuk {len(symbols)} koin. Mohon tunggu...")
    threads = []
    
    def worker(sym):
        if not init_historical_data(sym):
            # Jika gagal, buat default memori
            coin_data[sym] = {'prev_pct': 0.0, 'signal_sent': False}

    for sym in symbols:
        t = threading.Thread(target=worker, args=(sym,))
        threads.append(t)
        t.start()
        
    for t in threads:
        t.join()
        
    print("[√] Inisialisasi data selesai. Bot siap memindai secara real-time!")

def socket_callback(msg):
    """Handler data Real-Time dari WebSocket."""
    try:
        if 'data' not in msg: return
        d = msg['data']
        symbol = d['s']
        candle = d['k']
        
        is_closed = candle['x']
        c_open = float(candle['o'])
        c_curr = float(candle['c'])
        
        # Hitung persentase candle yang sedang berjalan SAAT INI (Detik ini)
        curr_pct = ((c_curr - c_open) / c_open) * 100
        
        if symbol not in coin_data:
            return

        prev_pct = coin_data[symbol]['prev_pct']
        signal_sent = coin_data[symbol]['signal_sent']

        # ==========================================================
        # LOGIKA FAST SCANNER (REAL-TIME TRIGGER)
        # Jika Candle 1 > 1% DAN Candle 2 menyentuh > 2% SAAT INI
        # ==========================================================
        if prev_pct > 1.0 and curr_pct > 2.0 and not signal_sent:
            print(f"⚡ INSTANT SIGNAL: {symbol} | Prev: {prev_pct:.2f}% | Now: {curr_pct:.2f}%")
            
            alert_msg = (
                f"⚡ *FAST SCALPING SIGNAL* ⚡\n\n"
                f"💰 *Koin:* #{symbol}\n"
                f"⏱️ *Timeframe:* 1 Menit (1m)\n"
                f"🟢 *Candle Sebelumnya:* `+{prev_pct:.2f}%`\n"
                f"🚀 *Candle Berjalan:* `+{curr_pct:.2f}%` (Real-Time)\n"
                f"💵 *Harga Breakout:* `{c_curr}`\n\n"
                f"⚠️ _Sinyal instan. Harga sedang melesat naik!_"
            )
            send_telegram(alert_msg)
            
            # Kunci sinyal agar tidak spam berkali-kali di candle yang sama
            coin_data[symbol]['signal_sent'] = True

        # Jika candle 1 menit ini sudah tertutup, simpan datanya untuk candle berikutnya
        if is_closed:
            coin_data[symbol]['prev_pct'] = curr_pct
            # Reset pengunci sinyal untuk candle menit berikutnya
            coin_data[symbol]['signal_sent'] = False

    except Exception as e:
        pass

def run_fast_scanner():
    print("==============================================")
    print("   🚀 BINANCE ULTRA-FAST MOMENTUM SCANNER")
    print("   [Mode: 1 Menit & Real-Time Trigger]")
    print("==============================================")
    
    # Ambil semua simbol futures yang aktif
    try:
        info = client.futures_exchange_info()
        symbols = [s['symbol'] for s in info['symbols'] if s['quoteAsset'] == 'USDT' and s['status'] == 'TRADING']
    except Exception as e:
        print(f"[!] Gagal mengambil market: {e}")
        return

    # Inisialisasi data historis (1 menit ke belakang)
    init_all_coins(symbols)

    # Siapkan koneksi WebSocket (1 menit TF)
    streams = [f"{s.lower()}@kline_1m" for s in symbols]
    twm = ThreadedWebsocketManager(api_key=BINANCE_API_KEY, api_secret=BINANCE_SECRET_KEY)
    twm.start()

    # Pecah stream menjadi chunk berisi 200 (Batas Binance)
    chunk_size = 200
    for i in range(0, len(streams), chunk_size):
        chunk = streams[i:i + chunk_size]
        twm.start_futures_multiplex_socket(callback=socket_callback, streams=chunk)
        time.sleep(1)

    print(f"[*] {len(streams)} Streams WebSocket Aktif! Memantau lonjakan harga...")

if __name__ == "__main__":
    try:
        run_fast_scanner()
        while True:
            time.sleep(10)
    except KeyboardInterrupt:
        print("\n[!] Bot dihentikan.")
