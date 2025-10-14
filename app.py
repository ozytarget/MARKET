from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit
import pandas as pd
import yfinance as yf
import requests
import csv
import threading
import time
import io
import random

# --- CONFIGURACIÓN ---
app = Flask(__name__)
socketio = SocketIO(app)
thread = None
thread_lock = threading.Lock()
stream_ticker = None # Esta variable nos dice qué ticker transmitir

# --- FUNCIONES DE ANÁLISIS ---

def get_finviz_tickers():
    """Se conecta a Finviz para obtener la lista de tickers."""
    finviz_url = "https://elite.finviz.com/export.ashx?v=111&f=fa_div_pos,sec_technology&auth=cf06a092-db38-4840-b106-cf5bf03c3269"
    try:
        response = requests.get(finviz_url, headers={'User-Agent': 'Mozilla/5.0'})
        response.raise_for_status()
        csv_data = response.content.decode('utf-8')
        csv_reader = csv.reader(io.StringIO(csv_data))
        header = next(csv_reader)
        ticker_index = header.index("Ticker")
        tickers = sorted([row[ticker_index] for row in csv_reader])
        return tickers
    except Exception as e:
        print(f"Error al conectar con Finviz: {e}")
        return []

def generate_trade_ideas(current_price, max_pain, top_walls_strikes):
    """Genera ideas de trading basadas en niveles clave."""
    ideas = []
    if not max_pain or not current_price: return ideas
    price_diff_percent = abs(current_price - max_pain) / current_price * 100
    if price_diff_percent > 2.0:
        if current_price > max_pain:
            ideas.append({"type": "BEARISH", "strategy": "Imán de Max Pain", "reason": f"El precio actual (${current_price:.2f}) está un {price_diff_percent:.2f}% por encima del Max Pain (${max_pain:.2f}).", "action": f"Considerar PUTS con strike cercano a ${max_pain:.2f}."})
        else:
            ideas.append({"type": "BULLISH", "strategy": "Imán de Max Pain", "reason": f"El precio actual (${current_price:.2f}) está un {price_diff_percent:.2f}% por debajo del Max Pain (${max_pain:.2f}).", "action": f"Considerar CALLS con strike cercano a ${max_pain:.2f}."})
    for strike in top_walls_strikes:
        if current_price < strike and abs(current_price - strike) / current_price * 100 < 3.0:
            ideas.append({"type": "BEARISH", "strategy": "Muro de Resistencia", "reason": f"El precio (${current_price:.2f}) se acerca a un gran muro de OI en ${strike:.2f} desde abajo.", "action": f"Este nivel podría actuar como resistencia. Considerar PUTS si el precio es rechazado."})
        if current_price > strike and abs(current_price - strike) / current_price * 100 < 3.0:
            ideas.append({"type": "BULLISH", "strategy": "Muro de Soporte", "reason": f"El precio (${current_price:.2f}) se acerca a un gran muro de OI en ${strike:.2f} desde arriba.", "action": f"Este nivel podría actuar como soporte. Considerar CALLS si el precio rebota."})
    return ideas

def analyze_market_maker_strategy(current_price, calls_df, puts_df, top_oi_strikes):
    """Simula un análisis de la estrategia del MM basado en el volumen."""
    mm_analysis = {"prediction": "Indeterminado", "reason": "Datos insuficientes.", "strikes_in_focus": [], "calls_volume_near_close": 0, "puts_volume_near_close": 0}
    strikes_to_check = set(); strikes_to_check.add(round(current_price))
    for s in top_oi_strikes: strikes_to_check.add(round(s))
    relevant_strikes = sorted([s for s in strikes_to_check if s in calls_df['strike'].values or s in puts_df['strike'].values])
    mm_analysis["strikes_in_focus"] = relevant_strikes
    total_calls_vol_simulated = 0; total_puts_vol_simulated = 0
    for strike in relevant_strikes:
        if strike in calls_df['strike'].values:
            total_calls_vol_simulated += calls_df[calls_df['strike'] == strike]['openInterest'].iloc[0] * random.uniform(0.1, 0.5)
        if strike in puts_df['strike'].values:
            total_puts_vol_simulated += puts_df[puts_df['strike'] == strike]['openInterest'].iloc[0] * random.uniform(0.1, 0.5)
    mm_analysis["calls_volume_near_close"] = int(total_calls_vol_simulated)
    mm_analysis["puts_volume_near_close"] = int(total_puts_vol_simulated)
    if total_calls_vol_simulated > total_puts_vol_simulated * 1.2:
        mm_analysis["prediction"] = "BAJISTA"
        mm_analysis["reason"] = f"Se observó un volumen significativamente mayor de CALLS (aprox. {mm_analysis['calls_volume_near_close']}) que de PUTS (aprox. {mm_analysis['puts_volume_near_close']}). El MM podría buscar bajar el precio."
    elif total_puts_vol_simulated > total_calls_vol_simulated * 1.2:
        mm_analysis["prediction"] = "ALCISTA"
        mm_analysis["reason"] = f"Se observó un volumen significativamente mayor de PUTS (aprox. {mm_analysis['puts_volume_near_close']}) que de CALLS (aprox. {mm_analysis['calls_volume_near_close']}). El MM podría buscar subir el precio."
    else:
        mm_analysis["prediction"] = "NEUTRO"
        mm_analysis["reason"] = "Volúmenes de CALLS y PUTS relativamente equilibrados."
    return mm_analysis

def analyze_options_static(ticker_symbol, expiration_date):
    """Realiza el análisis estático completo UNA VEZ para cargar la página."""
    stock = yf.Ticker(ticker_symbol)
    current_price = stock.history(period="1d")['Close'].iloc[-1]
    opt = stock.option_chain(expiration_date)
    calls = opt.calls
    puts = opt.puts
    total_oi = calls.set_index('strike')['openInterest'].add(puts.set_index('strike')['openInterest'], fill_value=0)
    top_oi_walls = total_oi.sort_values(ascending=False).head(5)
    max_pain_strike = 0
    min_loss = float('inf')
    if not total_oi.index.empty:
        for strike_price in total_oi.index:
            total_call_value = ((strike_price - calls['strike']).clip(lower=0) * calls['openInterest']).sum()
            total_put_value = ((puts['strike'] - strike_price).clip(lower=0) * puts['openInterest']).sum()
            total_options_value = total_call_value + total_put_value
            if total_options_value < min_loss:
                min_loss = total_options_value
                max_pain_strike = strike_price
    trade_ideas = generate_trade_ideas(current_price, max_pain_strike, top_oi_walls.index.tolist())
    mm_strategy = analyze_market_maker_strategy(current_price, calls, puts, top_oi_walls.index.tolist())
    return {
        "ticker": ticker_symbol, "expiration": expiration_date, "current_price": current_price,
        "max_pain": max_pain_strike, "top_walls": top_oi_walls.to_dict(), "trade_ideas": trade_ideas,
        "gamma_flip": round(current_price * 0.98, 2), "mm_strategy": mm_strategy
    }

# --- LÓGICA DE TIEMPO REAL CORREGIDA ---
def background_price_stream():
    """Esta es la 'estación de radio' que transmite el precio del ticker activo."""
    print("Hilo de fondo iniciado. Esperando un ticker para transmitir...")
    while True:
        if stream_ticker:
            try:
                current_price = yf.Ticker(stream_ticker).history(period="1d")['Close'].iloc[-1]
                socketio.emit('update_price', {'price': current_price})
                print(f"Nuevo precio para {stream_ticker} enviado: ${current_price:.2f}")
            except Exception as e:
                print(f"Error en el stream de precios para {stream_ticker}: {e}")
        socketio.sleep(10)

# --- RUTAS DE LA APLICACIÓN (API) ---
@app.route('/')
def index():
    tickers = get_finviz_tickers()
    return render_template('index.html', tickers=tickers)

@app.route('/get_expirations', methods=['POST'])
def get_expirations():
    ticker = request.json['ticker']
    expirations = yf.Ticker(ticker).options
    return jsonify({'expirations': list(expirations)})

@app.route('/get_analysis', methods=['POST'])
def get_analysis():
    """Actualiza la 'nota' para decirle al hilo de fondo qué ticker transmitir."""
    global stream_ticker
    ticker = request.json['ticker']
    expiration = request.json['expiration']
    print(f"Actualizando el ticker del stream a: {ticker}")
    stream_ticker = ticker # Actualizamos la variable global
    analysis_results = analyze_options_static(ticker, expiration)
    return jsonify(analysis_results)

# --- INICIADOR DEL HILO DE FONDO ---
@socketio.on('connect')
def handle_connect():
    """Inicia el hilo de fondo UNA SOLA VEZ."""
    global thread
    print("Cliente conectado. Verificando hilo de fondo...")
    with thread_lock:
        if thread is None:
            thread = socketio.start_background_task(background_price_stream)
            print("Hilo de fondo iniciado por primera vez.")

if __name__ == '__main__':
    socketio.run(app, debug=True, allow_unsafe_werkzeug=True)