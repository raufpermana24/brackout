import os
import time
import requests
import threading
import io
import numpy as np
import pandas as pd
import mplfinance as mpf
from binance.client import Client
from binance import ThreadedWebsocketManager

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

# Flag indikator untuk reconnect websocket
ws_needs_restart = False

def get_chart_image(symbol, tf_key):
    """Mengambil data historis dan menggambar chart Candlestick beserta Indikator."""
    try:
        interval = TIMEFRAMES[tf_key]
        # Ambil 60 candle terakhir untuk digambar
        klines = client.futures_klines(symbol=symbol, interval=interval, limit=60)
        
        # Format ke Pandas DataFrame
        df = pd.DataFrame(klines, columns=['Date', 'Open', 'High', 'Low', 'Close', 'Volume', 'c_time', 'q_av', 'trades', 'tb_base', 'tb_quote', 'ignore'])
        df['Date'] = pd.to_datetime(df['Date'], unit='ms')
        df.set_index('Date', inplace=True)
        
        for col in ['Open', 'High', 'Low', 'Close', 'Volume']:
            df[col] = df[col].astype(float)
            
        # --- PERHITUNGAN INDIKATOR ---
        # 1. Hitung RSI (14) menggunakan Exponential Moving Average (Wilder's Method)
        delta = df['Close'].diff()
        gain = delta.clip(lower=0)
        loss = -1 * delta.clip(upper=0)
        ema_gain = gain.ewm(com=13, adjust=False).mean()
        ema_loss = loss.ewm(com=13, adjust=False).mean()
        rs = ema_gain / ema_loss
        df['RSI'] = 100 - (100 / (1 + rs))

        # 2. Hitung OBV (On-Balance Volume)
        # Jika harga naik, volume ditambah. Jika turun, volume dikurang.
        df['OBV'] = (np.sign(df['Close'].diff()) * df['Volume']).fillna(0).cumsum()

        # Buat buffer gambar di memori
        buf = io.BytesIO()
        
        # Konfigurasi gaya grafik mplfinance
        mc = mpf.make_marketcolors(up='g', down='r', edge='inherit', wick='inherit', volume='in')
        s  = mpf.make_mpf_style(marketcolors=mc, gridstyle=':')
        
        # Tambahkan indikator RSI dan OBV sebagai panel tambahan (subplots) di bawah chart utama
        apdict = [
            mpf.make_addplot(df['RSI'], panel=1, color='blue', ylabel='RSI (14)'),
            # Tambahkan garis putus-putus untuk batas Overbought (70) dan Oversold (30)
            mpf.make_addplot([70]*len(df), panel=1, color='red', linestyle='dashed', alpha=0.5), 
            mpf.make_addplot([30]*len(df), panel=1, color='green', linestyle='dashed', alpha=0.5),
            
            mpf.make_addplot(df['OBV'], panel=2, color='orange', ylabel='OBV')
        ]
        
        # Gambar grafik dan simpan ke buffer
        # panel_ratios=(4, 1, 1) berarti: Chart utama lebih besar, sementara RSI dan OBV di bawahnya lebih kecil
        mpf.plot(
            df, type='candle', style=s, title=f"{symbol} | {tf_key}", 
            savefig=buf, figsize=(10, 8), addplot=apdict, panel_ratios=(4, 1, 1)
        )
        buf.seek(0)
        
        return buf
    except Exception as e:
        print(f"[!] Gagal membuat chart untuk {symbol}: {e}")
        return None

def send_telegram(message, symbol=None, tf_key=None):
    """Mengirim pesan (dan gambar jika tersedia) ke Telegram."""
    try:
        # Jika ada symbol dan tf_key, coba buat dan kirim foto grafik
        if symbol and tf_key:
            photo_buf = get_chart_image(symbol, tf_key)
            if photo_buf:
                url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
                payload = {"chat_id": TELEGRAM_CHAT_ID, "caption": message, "parse_mode": "Markdown"}
                files = {"photo": (f"{symbol}_chart.png", photo_buf, "image/png")}
                
                requests.post(url, data=payload, files=files, timeout=15)
                return # Sukses kirim gambar, keluar fungsi
                
        # Fallback: Jika gagal buat chart atau data tidak lengkap, kirim teks saja
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"[!] Error Telegram: {e}")

def get_historical_data(symbol, tf_key):
    """Fungsi Fallback: Mengambil data via REST API jika WebSocket belum mencukupi."""
    try:
        interval = TIMEFRAMES[tf_key]
        # Ambil 6 candle terakhir sesuai permintaan
        klines = client.futures_klines(symbol=symbol, interval=interval, limit=6)
        if len(klines) < 6: return None
        
        candles = []
        for k in klines:
            # k[1] adalah Open, k[4] adalah Close
            candles.append({'open': float(k[1]), 'close': float(k[4])})
        
        return candles
    except Exception as e:
        return None

def check_signal(symbol, tf_key, candles):
    """Logika utama: 3 Candle Terakhir Harus Hijau"""
    if len(candles) < 3: return
    
    # Ambil 3 candle terakhir yang sudah close dari memori (maks 6 candle)
    c1 = candles[-3]
    c2 = candles[-2]
    c3 = candles[-1]
    
    # Cek apakah ketiganya hijau (Close > Open)
    if c1['close'] > c1['open'] and c2['close'] > c2['open'] and c3['close'] > c3['open']:
        # Hitung total persentase kenaikan dari Open C1 ke Close C3
        total_pct = ((c3['close'] - c1['open']) / c1['open']) * 100
        
        # Hitung kenaikan per candle untuk informasi di Telegram
        pct1 = ((c1['close'] - c1['open']) / c1['open']) * 100
        pct2 = ((c2['close'] - c2['open']) / c2['open']) * 100
        pct3 = ((c3['close'] - c3['open']) / c3['open']) * 100
        
        print(f"🚀 SIGNAL [{tf_key}] {symbol}: 3 GREEN CANDLES (+{total_pct:.2f}%)")
        msg = (
            f"🚀 *3 GREEN CANDLES DETECTED: {symbol}*\n\n"
            f"⏱️ *Timeframe:* {tf_key}\n"
            f"📈 *Total Kenaikan:* `+{total_pct:.2f}%`\n\n"
            f"1️⃣ *Hijau 1:* `+{pct1:.2f}%`\n"
            f"2️⃣ *Hijau 2:* `+{pct2:.2f}%`\n"
            f"3️⃣ *Hijau 3:* `+{pct3:.2f}%`\n\n"
            f"💰 *Last Price:* `{c3['close']}`\n"
            f"🔗 [Binance Chart](https://www.binance.com/en/futures/{symbol})"
        )
        # Panggil send_telegram dengan info koin untuk digenerate gambarnya
        send_telegram(msg, symbol, tf_key)

def socket_callback(msg):
    """Handler data dari WebSocket."""
    global ws_needs_restart
    try:
        # Menangkap error putus koneksi "Read loop has been closed..."
        if isinstance(msg, dict) and msg.get('e') == 'error':
            print(f"\n[!] WebSocket Terputus/Error Terdeteksi: {msg.get('m')}")
            ws_needs_restart = True
            return

        if 'data' not in msg: return
        d = msg['data']
        symbol = d['s']
        candle = d['k']
        tf_code = candle['i']
        is_closed = candle['x']
        
        tf_key = next((k for k, v in TIMEFRAMES.items() if v == tf_code), None)
        if not tf_key: return

        if is_closed:
            open_p = float(candle['o'])
            close_p = float(candle['c'])
            new_candle = {'open': open_p, 'close': close_p}
            
            if symbol not in analysis_data[tf_key]:
                print(f"[*] Initializing {symbol} {tf_key} via REST API...")
                hist_candles = get_historical_data(symbol, tf_key)
                if hist_candles:
                    analysis_data[tf_key][symbol] = hist_candles
                    check_signal(symbol, tf_key, analysis_data[tf_key][symbol])
            else:
                analysis_data[tf_key][symbol].append(new_candle)
                # Jaga agar memori tidak bengkak, batasi hanya 6 candle terakhir
                if len(analysis_data[tf_key][symbol]) > 6:
                    analysis_data[tf_key][symbol] = analysis_data[tf_key][symbol][-6:]
                
                check_signal(symbol, tf_key, analysis_data[tf_key][symbol])
    except Exception as e:
        pass

# --- FUNGSI TAMBAHAN: SCAN HISTORICAL 2 HARI ---
def scan_historical_signals_2_days(symbols):
    """Mencari sinyal dari 2 hari ke belakang dan mengirimkannya."""
    print("[*] Memulai pemindaian data historis 2 hari ke belakang...")
    
    limit_map = {
        '15m': 48 * 4,
        '1h': 48,
        '4h': 12
    }
    
    for symbol in symbols:
        for tf_key, interval in TIMEFRAMES.items():
            try:
                limit = limit_map[tf_key]
                klines = client.futures_klines(symbol=symbol, interval=interval, limit=limit)
                
                if len(klines) < 3:
                    continue
                    
                for i in range(2, len(klines) - 1): # Butuh 3 candle (i-2, i-1, i)
                    c1_open = float(klines[i-2][1])
                    c1_close = float(klines[i-2][4])
                    
                    c2_open = float(klines[i-1][1])
                    c2_close = float(klines[i-1][4])
                    
                    c3_open = float(klines[i][1])
                    c3_close = float(klines[i][4])
                    
                    # Syarat mutlak: Ketiga candle harus hijau
                    if c1_close > c1_open and c2_close > c2_open and c3_close > c3_open:
                        total_pct = ((c3_close - c1_open) / c1_open) * 100
                        pct1 = ((c1_close - c1_open) / c1_open) * 100
                        pct2 = ((c2_close - c2_open) / c2_open) * 100
                        pct3 = ((c3_close - c3_open) / c3_open) * 100
                        
                        close_time = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(klines[i][6] / 1000))
                        price = c3_close
                        
                        print(f"🕰️ HISTORICAL SIGNAL [{tf_key}] {symbol} at {close_time}")
                        msg = (
                            f"🕰️ *HISTORICAL SIGNAL (Past 2 Days)*\n\n"
                            f"💎 *Symbol:* #{symbol}\n"
                            f"⏱️ *Timeframe:* {tf_key}\n"
                            f"📅 *Waktu Close:* {close_time}\n"
                            f"📈 *Total Kenaikan:* `+{total_pct:.2f}%`\n\n"
                            f"1️⃣ *Hijau 1:* `+{pct1:.2f}%`\n"
                            f"2️⃣ *Hijau 2:* `+{pct2:.2f}%`\n"
                            f"3️⃣ *Hijau 3:* `+{pct3:.2f}%`\n\n"
                            f"💰 *Price (Then):* `{price}`\n"
                            f"🔗 [Binance Chart](https://www.binance.com/en/futures/{symbol})"
                        )
                        # Panggil send_telegram dengan parameter koin untuk generate gambar
                        send_telegram(msg, symbol, tf_key)
                        time.sleep(1) # Jeda agar tidak spam saat mengirim banyak foto historis
                        
                time.sleep(0.05)
            except Exception as e:
                pass
                
    print("[*] ✅ Pemindaian data historis 2 hari telah selesai.")

def start_websocket(streams):
    """Membuka dan memulai ThreadedWebsocketManager."""
    twm = ThreadedWebsocketManager(api_key=BINANCE_API_KEY, api_secret=BINANCE_SECRET_KEY)
    twm.start()
    twm.start_multiplex_socket(callback=socket_callback, streams=streams)
    return twm

def run_scanner():
    global ws_needs_restart
    print("=== BINANCE HYBRID MULTI-TF SCANNER STARTED ===")
    print(f"Monitoring: {list(TIMEFRAMES.keys())}")
    
    try:
        info = client.futures_exchange_info()
        symbols = [s['symbol'] for s in info['symbols'] if s['quoteAsset'] == 'USDT' and s['status'] == 'TRADING']
        print(f"[*] Scanning {len(symbols)} market koin...")
    except Exception as e:
        print(f"[!] Gagal ambil daftar simbol: {e}")
        return

    # Mulai pemindaian history di background thread
    threading.Thread(target=scan_historical_signals_2_days, args=(symbols,), daemon=True).start()

    # Siapkan daftar stream untuk websocket
    streams = []
    for s in symbols:
        for tf in TIMEFRAMES.values():
            streams.append(f"{s.lower()}@kline_{tf}")
    
    # Inisialisasi awal WebSocket
    twm = start_websocket(streams)

    # Loop utama bot dan penjaga kestabilan WebSocket
    try:
        while True:
            if ws_needs_restart:
                print("[*] Melakukan Reset pada koneksi WebSocket...")
                try:
                    twm.stop() # Hentikan socket lama
                except:
                    pass
                
                time.sleep(5) # Beri jeda 5 detik agar port benar-benar tertutup bersih
                
                # Memulai ulang koneksi websocket
                twm = start_websocket(streams)
                ws_needs_restart = False
                print("[*] ✅ WebSocket berhasil disambung ulang dan berjalan kembali!")
                
            time.sleep(5)
    except KeyboardInterrupt:
        print("\n[!] Bot stopped secara manual.")
        try:
            twm.stop()
        except:
            pass

if __name__ == "__main__":
    run_scanner()
