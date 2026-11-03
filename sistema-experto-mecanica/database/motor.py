import psycopg2
import psycopg2.extras
import re
import os
from dotenv import load_dotenv

load_dotenv()

# Configuración de las credenciales de PostgreSQL de forma segura
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_NAME = os.getenv("DB_NAME", "mecanicadb")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASS = os.getenv("DB_PASS", "123")
DB_PORT = os.getenv("DB_PORT", "5432")

def get_connection():
    """Establece y retorna la conexión a PostgreSQL usando las variables globales."""
    try:
        conn = psycopg2.connect(
            host=DB_HOST,
            database=DB_NAME,
            user=DB_USER,
            password=DB_PASS,
            port=DB_PORT
        )
        return conn
    except Exception as e:
        print(f"Error crítico al conectar a la base de datos: {e}")
        return None
    
    
def guardar_historial(origen, resultado, nivel_coincidencia):
    """Guarda el resultado de un diagnóstico en la base de datos."""
    conn = get_connection()
    if not conn: return False
    
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO historial_diagnosticos (origen, resultado, nivel_coincidencia)
            VALUES (%s, %s, %s)
        """, (origen, resultado, nivel_coincidencia))
        conn.commit()
        return True
    except Exception as e:
        print(f"Error al guardar el historial: {e}")
        return False
    finally:
        cur.close()
        conn.close()
        
def limpiar_historial():
    """Elimina todos los registros de la tabla historial_diagnosticos y reinicia el contador."""
    conn = get_connection()
    if not conn: return False
    
    try:
        cur = conn.cursor()
        cur.execute("TRUNCATE TABLE historial_diagnosticos RESTART IDENTITY;")
        conn.commit()
        return True
    except Exception as e:
        print(f"Error al limpiar el historial: {e}")
        return False
    finally:
        cur.close()
        conn.close()

def obtener_historial():
    """Obtiene los últimos 50 diagnósticos ordenados por fecha."""
    conn = get_connection()
    if not conn: return []
    
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute("""
            SELECT id_historial, 
                   to_char(fecha, 'DD/MM/YYYY<br>HH12:MI a.m.') as fecha_fmt, 
                   origen, 
                   resultado, 
                   nivel_coincidencia
            FROM historial_diagnosticos
            ORDER BY fecha DESC
            LIMIT 50
        """)
        return [dict(row) for row in cur.fetchall()]
    except Exception as e:
        print(f"Error al obtener el historial: {e}")
        return []
    finally:
        cur.close()
        conn.close()

def procesar_mensaje_chat(mensaje_usuario):
    """
    Detecta si el usuario ingresó un código OBD-II (P, B, C, U seguido de 4 caracteres)
    y ejecuta la inferencia por código exacto y por familia de subsistema.
    """
    # Regex para capturar códigos estándar como P0300, U0100, C1234, p0171, etc.
    match = re.search(r'\b([PBCUpbcu][0-3][0-9A-Fa-f]{3})\b', mensaje_usuario)
    
    if not match:
        return {
            "tipo": "conversacion",
            "respuesta": "Por favor, ingresa un código de escáner OBD-II válido (por ejemplo: <b>P0300</b>, <b>P0171</b>, <b>P0420</b> o <b>U0100</b>) para analizar la falla."
        }

    codigo = match.group(1).upper()
    prefijo = codigo[:3]      # Ej: 'P03'
    letra_sistema = codigo[0] # Ej: 'P'
    tipo_codigo = codigo[1]   # '0' Genérico, '1' Fabricante

    conn = get_connection()
    if not conn:
        return {"tipo": "error", "respuesta": "Error de conexión con la base de conocimientos."}

    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        
        # 1. Buscar la descripción exacta en los +3,000 códigos
        cur.execute("SELECT descripcion FROM codigos_obd WHERE codigo = %s", (codigo,))
        row_codigo = cur.fetchone()
        descripcion_exacta = row_codigo['descripcion'] if row_codigo else "Código específico de fabricante o variación de sensor."

        # 2. Inferencia por Familia: Primero intenta los 3 caracteres exactos (ej. 'P03')
        cur.execute("SELECT * FROM familias_obd WHERE prefijo = %s", (prefijo,))
        familia = cur.fetchone()

        # 3. Si no está el prefijo exacto (ej. C1234 -> mapea a C10, B1500 -> B10)
        if not familia:
            prefijo_familia_general = codigo[:2] + '0'
            cur.execute("SELECT * FROM familias_obd WHERE prefijo = %s", (prefijo_familia_general,))
            familia = cur.fetchone()

        # 4. Asignar valores desde la base de datos o usar respaldo general por letra (P, C, B, U)
        if familia:
            sub = familia['nombre_subsistema']
            urg = familia['urgencia']
            pasos_lista = familia['pasos_diagnostico'].split('|')
            causas = familia['posibles_causas']
        else:
            reglas_generales = {
                'P': ('Tren Motriz (Motor y Transmisión)', 'Media-Alta', '1. Verificar arnés del sensor indicado.|2. Revisar valores en vivo (Live Data) con el escáner.|3. Comprobar alimentación de 5V y tierra de la ECU.'),
                'C': ('Chasis (Frenos ABS, Tracción y Suspensión)', 'Alta', '1. Inspeccionar sensores de velocidad de rueda (WSS).|2. Revisar nivel de líquido de frenos y fusibles del módulo ABS.|3. Limpiar anillos reluctores.'),
                'B': ('Carrocería y Confort (Airbags, A/C, Tablero)', 'Baja-Media', '1. Revisar fusibles de cabina (BCM).|2. Verificar conectores debajo de los asientos (si es código SRS/Airbag).'),
                'U': ('Red de Comunicación (CAN Bus)', 'Alta', '1. Medir resistencia de la red CAN (debe dar 60 Ohmios entre pines 6 y 14 del conector OBD).|2. Verificar voltaje de batería (voltaje bajo genera códigos U falsos).')
            }
            sub, urg, pasos = reglas_generales.get(letra_sistema, reglas_generales['P'])
            pasos_lista = pasos.split('|')
            causas = "Falla eléctrica en el circuito del sensor, conector sulfatado o módulo desprogramado."

        # 5. Inteligencia extra: Si termina en número de cilindro (P0301 a P0312)
        nota_extra = ""
        if prefijo == 'P03' and codigo[3:].isdigit() and 0 < int(codigo[3:]) <= 12:
            nota_extra = f"⚡ <b>Detección específica:</b> La falla se localiza exactamente en el <b>Cilindro #{int(codigo[3:])}</b>."

        return {
            "tipo": "diagnostico_obd",
            "codigo": codigo,
            "alcance": "Estándar Universal (SAE)" if tipo_codigo == '0' else "Específico del Fabricante (OEM)",
            "descripcion": descripcion_exacta,
            "subsistema": sub,
            "urgencia": urg,
            "causas": causas,
            "pasos": pasos_lista,
            "nota_extra": nota_extra
        }

    except Exception as e:
        print(f"Error en procesar_mensaje_chat: {e}")
        return {"tipo": "error", "respuesta": "Ocurrió un error al consultar la base de conocimientos."}
    finally:
        cur.close()
        conn.close()

def obtener_sintomas_por_categoria(id_subsistema):
    """Extrae todas las preguntas (síntomas) asociadas a una categoría específica."""
    conn = get_connection()
    if not conn:
        print("¡Error: No se pudo conectar a la base de datos!")
        return []
    
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute("""
            SELECT id_sintoma, pregunta_usuario, codigo_interno 
            FROM sintomas 
            WHERE id_subsistema = %s 
            ORDER BY id_sintoma ASC
        """, (id_subsistema,))
        
        sintomas = cur.fetchall()
        print(f"Síntomas encontrados en DB: {len(sintomas)}")
        return [dict(row) for row in sintomas]
    except Exception as e:
        print(f"Error al obtener los síntomas: {e}")
        return []
    finally:
        cur.close()
        conn.close()

def evaluar_diagnostico(sintomas_confirmados_ids, id_subsistema):
    """
    Cruza los síntomas confirmados por el usuario ('Sí') contra las reglas
    de la base de conocimiento y retorna la falla con mayor factor de certeza.
    """
    conn = get_connection()
    if not conn:
        return None
    
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        
        # 1. Obtener todas las reglas correspondientes al subsistema evaluado
        cur.execute("""
            SELECT r.id_regla, r.factor_certeza, f.nombre_falla, f.urgencia, f.recomendacion
            FROM reglas r
            JOIN fallas f ON r.id_falla = f.id_falla
            WHERE f.id_subsistema = %s
        """, (id_subsistema,))
        
        reglas = cur.fetchall()
        mejor_diagnostico = None
        mayor_certeza = 0.0

        # 2. Evaluar cada regla iterativamente
        for regla in reglas:
            id_regla = regla['id_regla']
            
            # Obtener las condiciones (SI-ENTONCES) necesarias para activar esta regla
            cur.execute("""
                SELECT id_sintoma, valor_esperado 
                FROM regla_condiciones 
                WHERE id_regla = %s
            """, (id_regla,))
            condiciones = cur.fetchall()
            
            cumple_regla = True
            
            # 3. Verificar si el patrón de respuestas del usuario coincide exactamente
            for cond in condiciones:
                sintoma_id = cond['id_sintoma']
                valor_esperado = cond['valor_esperado']
                
                sintoma_presente = sintoma_id in sintomas_confirmados_ids
                
                if sintoma_presente != valor_esperado:
                    cumple_regla = False
                    break # Si una sola condición falla, se descarta toda la regla
            
            # 4. Si la regla se cumple y su certeza es superior a las anteriores, se guarda como resultado final
            if cumple_regla:
                certeza_actual = float(regla['factor_certeza'])
                if certeza_actual > mayor_certeza:
                    mayor_certeza = certeza_actual
                    mejor_diagnostico = {
                        "falla": regla['nombre_falla'],
                        "certeza": int(certeza_actual * 100),
                        "urgencia": regla['urgencia'],
                        "recomendacion": regla['recomendacion']
                    }
        
        return mejor_diagnostico
    

    except Exception as e:
        print(f"Error durante el motor de inferencia: {e}")
        return None
    finally:
        cur.close()
        conn.close()