import os
import sys
from flask import Flask, render_template, request, jsonify, session

# Configuramos rutas para importar el módulo de base de datos
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
PROJECT_ROOT = os.path.join(BASE_DIR, '..')
FRONTEND_DIR = os.path.join(PROJECT_ROOT, 'frontend')
sys.path.append(PROJECT_ROOT)

from database import motor as db

app = Flask(__name__, template_folder=FRONTEND_DIR)
app.secret_key = 'super_secreta_uees_2026'

# --- RUTAS DE LAS PANTALLAS ---

@app.route('/chatbot')
def chatbot():
    return render_template('chatbot.html')

@app.route('/')
def inicio():
    # Limpiamos todo rastro del diagnóstico anterior para permitir empezar de nuevo
    session.clear()
    return render_template('inicio.html')

@app.route('/diagnostico')
def diagnostico():
    categoria_elegida = request.args.get('categoria', 'general')
    session.clear()
    
    # Mapeamos el nombre de la categoría al id_subsistema de la base de datos
    mapa_categorias = {'arranque': 1, 'frenos': 2, 'motor': 3}
    id_sub = mapa_categorias.get(categoria_elegida, 1)
    
    # Guardamos en sesión por si también usas el cuestionario clásico
    sintomas_db = db.obtener_sintomas_por_categoria(id_sub)
    session['sintomas_pendientes'] = sintomas_db
    session['sintomas_confirmados'] = []
    session['id_subsistema'] = id_sub
    session['categoria_actual'] = categoria_elegida
    
    return render_template('diagnostico.html', categoria=categoria_elegida)

@app.route('/resultado')
def resultado():
    falla = session.get('falla_detectada', 'Falla desconocida')
    certeza = session.get('certeza', '0%')
    recomendacion = session.get('recomendacion', 'Consulta a tu mecánico de confianza.')
    return render_template('resultado.html', falla=falla, certeza=certeza, recomendacion=recomendacion)

@app.route('/consejos')
def consejos():
    return render_template('consejos.html')

@app.route('/historial')
def historial():
    # Obtenemos los registros reales de PostgreSQL
    registros = db.obtener_historial()
    return render_template('historial.html', registros=registros)

@app.route('/acerca')
def acerca():
    return render_template('acerca.html')


# --- RUTAS DE LA API (MOTOR DE INFERENCIA Y CHATBOT OBD-II) ---

@app.route('/api/chat', methods=['POST'])
def api_chat():
    data = request.get_json(silent=True) or {}
    mensaje = data.get('mensaje', '').strip()
    
    if not mensaje:
        return jsonify({"tipo": "error", "respuesta": "Escribe un código de escáner OBD-II para analizar."})
        
    resultado = db.procesar_mensaje_chat(mensaje)
    
    # Guardamos el último diagnóstico del chat en la sesión por si visita /resultado
    if resultado.get("tipo") == "diagnostico_obd":
        falla_str = f"{resultado['codigo']} - {resultado['descripcion']}"
        certeza_str = "100%" if resultado['alcance'].startswith("Estándar") else "85%"
        
        session['falla_detectada'] = falla_str
        session['certeza'] = certeza_str
        session['recomendacion'] = " ".join(resultado['pasos'])
        
        # Guardamos en la base de datos
        db.guardar_historial('Escáner OBD-II', falla_str, certeza_str)
        
    return jsonify(resultado)

@app.route('/api/pregunta', methods=['GET'])
def obtener_pregunta():
    pendientes = session.get('sintomas_pendientes', [])
    
    if pendientes:
        sintoma_actual = pendientes[0]
        return jsonify({"estado": "pregunta", "texto": sintoma_actual['pregunta_usuario']})
    else:
        return jsonify({"estado": "terminado"})

@app.route('/api/responder', methods=['POST'])
def responder():
    data = request.get_json(silent=True) or {}
    respuesta = data.get('respuesta')
    
    pendientes = session.get('sintomas_pendientes', [])
    confirmados = session.get('sintomas_confirmados', [])
    
    if not pendientes:
        return jsonify({"estado": "ok", "siguiente": "resultado"})
        
    sintoma_actual = pendientes.pop(0)
    
    if respuesta == 'si':
        confirmados.append(sintoma_actual['id_sintoma'])
        
    session['sintomas_pendientes'] = pendientes
    session['sintomas_confirmados'] = confirmados
    
    # Si al quitar esa pregunta la lista quedó vacía, es hora de evaluar el diagnóstico
    if not pendientes:
        id_sub = session.get('id_subsistema', 1)
        resultado = db.evaluar_diagnostico(confirmados, id_sub)
        
        if resultado:
            session['falla_detectada'] = resultado['falla']
            session['certeza'] = f"{resultado['certeza']}%"
            session['recomendacion'] = resultado['recomendacion']
            
            # Guardamos en la base de datos
            db.guardar_historial('Síntomas', resultado['falla'], f"{resultado['certeza']}%")
        else:
            session['falla_detectada'] = "Falla no identificada"
            session['certeza'] = "N/A"
            session['recomendacion'] = "Los síntomas no coinciden con una falla común exacta. Visita tu taller."
            
        return jsonify({"estado": "ok", "siguiente": "resultado"})
        
    return jsonify({"estado": "ok", "siguiente": "continuar"})

if __name__ == '__main__':
    app.run(debug=True)