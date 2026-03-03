import os
import time
import requests
import threading
import io
import json
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
DB_FILE = 'bot_memory.json'

# --- SISTEM MEMORI ---
# Struktur Memori: { '15m': { 'BTCUSDT': [{'open':.., 'close':.., 'close_time':..}] }, ... }
analysis_data = {tf: {} for tf in TIMEFRAMES.keys()}
client = Client(BINANCE_API_KEY, BINANCE_SECRET_KEY)
ws_needs_restart = False

# State untuk mencegah pengiriman sinyal berturut-turut pada streak hijau yang sama
signal_state = {tf: {} for tf in TIMEFRAMES.keys()} 

def load_memory():
    """Memuat data riwayat sinyal terakhir dari file JSON lokal."""
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, 'r') as f:
                return json.load(f)
        except Exception as e:
            print(f"[!] Error membaca database lokal: {e}")
    return {tf: {} for tf in TIMEFRAMES.keys()}

def save_memory():
    """Menyimpan data riwayat sinyal ke file JSON lokal."""
    try:
        with open(DB_FILE, 'w') as f:
            json.dump(last_signal_time, f)
    except Exception as e:
        print(f"[!] Error menyimpan database lokal: {e}")

# Memuat memori saat bot pertama kali dijalankan
last_signal_time = load_memory()

def get_chart_image(symbol, tf_key):
    """Mengambil data historis dan menggambar chart Candlestick beserta Indikator."""
    try:
        interval = TIMEFRAMES[tf_key]
        # Ambil 300 candle agar perhitungan EMA 233 memiliki data historis yang cukup untuk akurat
        klines = client.futures_klines(symbol=symbol, interval=interval, limit=300)
        
        df = pd.DataFrame(klines, columns=['Date', 'Open', 'High', 'Low', 'Close', 'Volume', 'c_time', 'q_av', 'trades', 'tb_base', 'tb_quote', 'ignore'])
        df['Date'] = pd.to_datetime(df['Date'], unit='ms')
        df.set_index('Date', inplace=True)
        
        for col in ['Open', 'High', 'Low', 'Close', 'Volume']:
            df[col] = df[col].astype(float)
            
        # Perhitungan Indikator RSI & OBV
        delta = df['Close'].diff()
        gain = delta.clip(lower=0)
        loss = -1 * delta.clip(upper=0)
        ema_gain = gain.ewm(com=13, adjust=False).mean()
        ema_loss = loss.ewm(com=13, adjust=False).mean()
        rs = ema_gain / ema_loss
        df['RSI'] = 100 - (100 / (1 + rs))
        df['OBV'] = (np.sign(df['Close'].diff()) * df['Volume']).fillna(0).cumsum()

        # --- PERHITUNGAN EMA MULTIPLE ---
        emas = [5, 8, 13, 21, 34, 55, 89, 144, 233]
        # Daftar warna untuk membedakan setiap garis EMA di chart
        ema_colors = ['#FF0000', '#FF7F00', '#DAA520', '#00FF00', '#0000FF', '#4B0082', '#9400D3', '#FF1493', '#00FFFF']
        for ema in emas:
            df[f'EMA_{ema}'] = df['Close'].ewm(span=ema, adjust=False).mean()

        # Potong data hanya untuk 80 candle terakhir agar chart tetap proporsional dan tidak berdempetan
        df_plot = df.iloc[-80:]

        buf = io.BytesIO()
        mc = mpf.make_marketcolors(up='g', down='r', edge='inherit', wick='inherit', volume='in')
        # Menambahkan background style 'nightclouds' agar garis EMA yang berwarna warni lebih kontras dan jelas
        s  = mpf.make_mpf_style(marketcolors=mc, gridstyle=':', base_mpf_style='nightclouds')
        
        apdict = []
        # Tambahkan semua garis EMA ke Panel 0 (Chart Candlestick Utama)
        for i, ema in enumerate(emas):
            apdict.append(mpf.make_addplot(df_plot[f'EMA_{ema}'], panel=0, color=ema_colors[i], width=1.0))
            
        # Tambahkan indikator RSI dan OBV ke panel bawahnya
        apdict.extend([
            mpf.make_addplot(df_plot['RSI'], panel=1, color='blue', ylabel='RSI (14)'),
            mpf.make_addplot([70]*len(df_plot), panel=1, color='red', linestyle='dashed', alpha=0.5), 
            mpf.make_addplot([30]*len(df_plot), panel=1, color='green', linestyle='dashed', alpha=0.5),
            mpf.make_addplot(df_plot['OBV'], panel=2, color='orange', ylabel='OBV')
        ])
        
        mpf.plot(
            df_plot, type='candle', style=s, title=f"{symbol} | {tf_key}", 
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
        if symbol and tf_key:
            photo_buf = get_chart_image(symbol, tf_key)
            if photo_buf:
                url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
                payload = {"chat_id": TELEGRAM_CHAT_ID, "caption": message, "parse_mode": "Markdown"}
                files = {"photo": (f"{symbol}_chart.png", photo_buf, "image/png")}
                
                requests.post(url, data=payload, files=files, timeout=15)
                return
                
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"[!] Error Telegram: {e}")

def get_historical_data(symbol, tf_key):
    """Fungsi Fallback: Mengambil data via REST API."""
    try:
        interval = TIMEFRAMES[tf_key]
        klines = client.futures_klines(symbol=symbol, interval=interval, limit=6)
        if len(klines) < 6: return None
        
        candles = []
        for k in klines:
            # Menyimpan open_time, close_time, open_price, close_price
            candles.append({
                'open_time': k[0], 
                'close_time': k[6], 
                'open': float(k[1]), 
                'close': float(k[4])
            })
        
        return candles
    except Exception as e:
        return None

def check_signal(symbol, tf_key, candles):
    """Logika utama dengan filter Anti-Duplikat dan Pelacak Struktur."""
    if len(candles) < 3: return
    
    c1, c2, c3 = candles[-3], candles[-2], candles[-1]
    
    is_green_c1 = c1['close'] > c1['open']
    is_green_c2 = c2['close'] > c2['open']
    is_green_c3 = c3['close'] > c3['open']
    
    # Jika candle terakhir merah, berarti struktur terputus (reset state)
    if not is_green_c3:
        signal_state[tf_key][symbol] = False
        return

    # Jika terbentuk 3 candle hijau beruntun
    if is_green_c1 and is_green_c2 and is_green_c3:
        # 1. Cek apakah sinyal sudah dikirim pada streak hijau ini
        if signal_state[tf_key].get(symbol, False):
            return 
            
        # 2. Cek apakah ini sinyal lama yang sudah tercatat di Database Lokal
        last_time = last_signal_time[tf_key].get(symbol, 0)
        if c3['close_time'] <= last_time:
            return 
            
        # Jika lolos kedua filter di atas, hitung persentase dan kirim!
        total_pct = ((c3['close'] - c1['open']) / c1['open']) * 100
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
            f"💰 *Last Price:* `{c3['close']}`\n\n"
            f"🎨 *Ket. Garis EMA:* 🔴5 🟠8 🟡13 🟢21 🔵34 🟣55 🟪89 💖144 🩵233\n"
            f"🔗 [Binance Chart](https://www.binance.com/en/futures/{symbol})"
        )
        send_telegram(msg, symbol, tf_key)
        
        # --- UPDATE MEMORI ---
        signal_state[tf_key][symbol] = True
        last_signal_time[tf_key][symbol] = c3['close_time']
        save_memory() # Simpan ke JSON

def socket_callback(msg):
    """Handler data dari WebSocket."""
    global ws_needs_restart
    try:
        if isinstance(msg, dict) and msg.get('e') == 'error':
            print(f"\n[!] WebSocket Terputus: {msg.get('m')}")
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
            new_candle = {
                'open_time': candle['t'],
                'close_time': candle['T'],
                'open': float(candle['o']), 
                'close': float(candle['c'])
            }
            
            if symbol not in analysis_data[tf_key]:
                hist_candles = get_historical_data(symbol, tf_key)
                if hist_candles:
                    analysis_data[tf_key][symbol] = hist_candles
                    check_signal(symbol, tf_key, analysis_data[tf_key][symbol])
            else:
                analysis_data[tf_key][symbol].append(new_candle)
                if len(analysis_data[tf_key][symbol]) > 6:
                    analysis_data[tf_key][symbol] = analysis_data[tf_key][symbol][-6:]
                
                check_signal(symbol, tf_key, analysis_data[tf_key][symbol])
    except Exception as e:
        pass

def scan_historical_signals_1_day(symbols):
    """Mencari sinyal TERBARU dari 1 hari ke belakang (Hanya 1 sinyal per Timeframe)."""
    print("[*] Memulai pemindaian data historis 1 hari ke belakang...")
    # Batas disesuaikan untuk 1 hari (24 jam)
    limit_map = {'15m': 96, '1h': 24, '4h': 6}
    
    for tf_key, interval in TIMEFRAMES.items():
        print(f"[*] Mencari histori 1 sinyal paling baru untuk Timeframe {tf_key}...")
        latest_signal = None
        
        for symbol in symbols:
            try:
                limit = limit_map[tf_key]
                klines = client.futures_klines(symbol=symbol, interval=interval, limit=limit)
                
                if len(klines) < 3: continue
                
                # Cari dari yang terbaru (mundur) ke yang paling tua
                for i in range(len(klines) - 2, 1, -1): 
                    c1_open, c1_close = float(klines[i-2][1]), float(klines[i-2][4])
                    c2_open, c2_close = float(klines[i-1][1]), float(klines[i-1][4])
                    c3_open, c3_close = float(klines[i][1]), float(klines[i][4])
                    c3_close_time = klines[i][6]
                    
                    if c1_close > c1_open and c2_close > c2_open and c3_close > c3_open:
                        # Cek database apakah ini sudah pernah dikirim sebelumnya
                        last_time = last_signal_time[tf_key].get(symbol, 0)
                        if c3_close_time <= last_time:
                            break # Lewati jika sudah ada di database lokal
                            
                        # Update jika sinyal ini LEBIH BARU daripada sinyal koin lain yang sudah ditemukan di loop
                        if latest_signal is None or c3_close_time > latest_signal['time']:
                            latest_signal = {
                                'symbol': symbol,
                                'time': c3_close_time,
                                'c3_close': c3_close,
                                'total_pct': ((c3_close - c1_open) / c1_open) * 100,
                                'pct1': ((c1_close - c1_open) / c1_open) * 100,
                                'pct2': ((c2_close - c2_open) / c2_open) * 100,
                                'pct3': ((c3_close - c3_open) / c3_open) * 100
                            }
                        break # Karena kita mencari dari belakang, ini pasti yang paling baru untuk koin INI. Lanjut ke koin lain.
                        
                time.sleep(0.05)
            except Exception as e:
                pass
                
        # --- KIRIM HANYA 1 SINYAL TERBARU UNTUK TIMEFRAME INI ---
        if latest_signal:
            sym = latest_signal['symbol']
            close_time = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(latest_signal['time'] / 1000))
            
            print(f"🕰️ HISTORICAL SIGNAL [{tf_key}] {sym} at {close_time}")
            msg = (
                f"🕰️ *HISTORICAL SIGNAL (Terbaru di {tf_key})*\n\n"
                f"💎 *Symbol:* #{sym}\n"
                f"⏱️ *Timeframe:* {tf_key}\n"
                f"📅 *Waktu Close:* {close_time}\n"
                f"📈 *Total Kenaikan:* `+{latest_signal['total_pct']:.2f}%`\n\n"
                f"1️⃣ *Hijau 1:* `+{latest_signal['pct1']:.2f}%`\n"
                f"2️⃣ *Hijau 2:* `+{latest_signal['pct2']:.2f}%`\n"
                f"3️⃣ *Hijau 3:* `+{latest_signal['pct3']:.2f}%`\n\n"
                f"💰 *Price (Then):* `{latest_signal['c3_close']}`\n\n"
                f"🎨 *Ket. Garis EMA:* 🔴5 🟠8 🟡13 🟢21 🔵34 🟣55 🟪89 💖144 🩵233\n"
                f"🔗 [Binance Chart](https://www.binance.com/en/futures/{sym})"
            )
            send_telegram(msg, sym, tf_key)
            
            # Simpan ke memori dan JSON agar tidak dikirim lagi nanti
            last_signal_time[tf_key][sym] = latest_signal['time']
            save_memory()
            time.sleep(1) 
            
    print("[*] ✅ Pemindaian data historis 1 hari telah selesai.")

def start_websocket(streams):
    """Membuka dan memulai ThreadedWebsocketManager dengan metode Chunking untuk menghindari HTTP 414."""
    twm = ThreadedWebsocketManager(api_key=BINANCE_API_KEY, api_secret=BINANCE_SECRET_KEY)
    twm.start()
    
    # Chunking: Pecah daftar streams agar tidak kepanjangan (maks 150 stream per socket)
    chunk_size = 150
    for i in range(0, len(streams), chunk_size):
        chunk = streams[i:i + chunk_size]
        twm.start_multiplex_socket(callback=socket_callback, streams=chunk)
        time.sleep(0.5) # Berikan jeda antar pembuatan socket
        
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

    # Jalankan pemindaian historis SECARA SINKRON (Tunggu histori selesai dulu baru lanjut)
    scan_historical_signals_1_day(symbols)

    streams = []
    for s in symbols:
        for tf in TIMEFRAMES.values():
            streams.append(f"{s.lower()}@kline_{tf}")
    
    twm = start_websocket(streams)

    try:
        while True:
            if ws_needs_restart:
                print("[*] Melakukan Reset pada koneksi WebSocket...")
                try: twm.stop() 
                except: pass
                time.sleep(5) 
                
                twm = start_websocket(streams)
                ws_needs_restart = False
                print("[*] ✅ WebSocket berhasil disambung ulang dan berjalan kembali!")
                
            time.sleep(5)
    except KeyboardInterrupt:
        print("\n[!] Bot stopped secara manual.")
        try: twm.stop()
        except: pass

if __name__ == "__main__":
    run_scanner()
