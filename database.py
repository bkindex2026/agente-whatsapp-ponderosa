import os
import psycopg2

DATABASE_URL = os.environ.get("DATABASE_URL")

def obtener_conexion():
    if not DATABASE_URL:
        raise ValueError("⚠️ DATABASE_URL no esta configurada en las variables de entorno.")
    return psycopg2.connect(DATABASE_URL)

def insertar_comprobante(data: dict) -> int:
    query = """
    INSERT INTO comprobantes (
        tipo_documento,
        serie_numero,
        ruc_emisor,
        razon_social_emisor,
        ruc_receptor,
        fecha_emision,
        hora_emision,
        galones,
        concepto_detalle,
        chofer,
        moneda,
        op_gravada,
        igv,
        total,
        placa_referencia,
        nombre_archivo,
        hash_contenido
    ) VALUES (
        %(tipo_documento)s,
        %(serie_numero)s,
        %(ruc_emisor)s,
        %(razon_social)s,
        %(ruc_receptor)s,
        %(fecha_emision)s,
        %(hora)s,
        %(galones)s,
        %(concepto)s,
        %(chofer)s,
        %(moneda)s,
        %(op_gravada)s,
        %(igv)s,
        %(total)s,
        %(placa)s,
        %(nombre_archivo)s,
        %(sha256_hash)s
    )
    RETURNING id;
    """
    
    data_mapeada = {
        "tipo_documento": data.get("tipo_documento") or "COMPROBANTE",
        "serie_numero": data.get("serie_numero") or "PENDIENTE_OCR",
        "ruc_emisor": data.get("ruc_emisor") or "00000000000",
        "razon_social": data.get("razon_social") or "DESCONOCIDO",
        "ruc_receptor": data.get("ruc_receptor") or "20601014193",
        "fecha_emision": data.get("fecha_emision") if data.get("fecha_emision") else None,
        "hora": data.get("hora"),
        "galones": float(data.get("galones") or 0.0),
        "concepto": data.get("concepto"),
        "chofer": data.get("chofer"),
        "moneda": data.get("moneda") or "PEN",
        "op_gravada": float(data.get("op_gravada") or 0.0),
        "igv": float(data.get("igv") or 0.0),
        "total": float(data.get("total") or 0.0),
        "placa": data.get("placa"),
        "nombre_archivo": data.get("nombre_archivo") or "comprobante.jpg",
        "sha256_hash": data.get("sha256_hash") or "N/A"
    }

    conn = obtener_conexion()
    cursor = conn.cursor()
    try:
        cursor.execute(query, data_mapeada)
        nuevo_id = cursor.fetchone()[0]
        conn.commit()
        print(f"🚀 EXITO: Registro insertado correctamente en Neon DB con el ID: {nuevo_id}")
        return nuevo_id
    except Exception as e:
        conn.rollback()
        print(f"❌ Error al ejecutar INSERT en PostgreSQL: {e}")
        raise e
    finally:
        cursor.close()
        conn.close()