import base64
import os
import requests

# 1. Definir la ruta de la factura real
# Primero buscaremos si la factura está en la carpeta de errores para re-evaluarla
RUTA_FACTURA = os.path.join("comprobantes_whatsapp", "errores", "factura")

if not os.path.exists(RUTA_FACTURA):
    # Intentar con la extensión .jpeg si existe
    RUTA_FACTURA = os.path.join("comprobantes_whatsapp", "errores", "factura.jpeg")
    if not os.path.exists(RUTA_FACTURA):
        RUTA_FACTURA = "factura.jpeg"

print(f"Leyendo archivo real desde: {RUTA_FACTURA}")

try:
    with open(RUTA_FACTURA, "rb") as f:
        bytes_archivo = f.read()
        base64_encoded = base64.b64encode(bytes_archivo).decode("utf-8")

    payload = {
        "data": {
            "pushName": "Víctor Tiburcio",
            "key": {"remoteJid": "Facturas ponderosa"},
            "message": {
                "documentMessage": {
                    "fileName": "factura_real.jpg",
                    "mimetype": "image/jpeg",
                    "base64": base64_encoded
                }
            }
        }
    }

    # PASO 65.2: URL actualizada con tu puerto público de Ngrok
    url = "https://agente-whatsapp-ponderosa.onrender.com/webhook/whatsapp"
    response = requests.post(url, json=payload)
    print("Respuesta servidor:", response.json())

except Exception as e:
    print(f"⚠️ Error leyendo o enviando el archivo: {e}")