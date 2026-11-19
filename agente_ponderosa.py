"""
agente_ponderosa.py - Versión 1.3 (Auditado Defensivo)
======================================================
Sistema Autónomo End-to-End para la Extracción, OCR Multimodal y Registro 
de Comprobantes Contables para Ponderosa Minerals S.A.C. (RUC 20601014193).

Historial de Versiones:
- v1.0: Estructura inicial del pipeline.
- v1.1: Incorporación de OCR con gemini-3.8-flash y deduplicación SHA-256.
- v1.2: Adición de manejadores de descarga para adjuntos PDF y blobs.
- v1.3: Control de latencia en WhatsApp Web, cadena de 4 fallbacks para la 
        apertura de chats y control estricto de recursos Chrome.
"""

import os
import re
import sys
import time
import json
import hashlib
import logging
import shutil
import base64
import subprocess
from pathlib import Path
from typing import Optional, List, Dict, Any
from datetime import datetime

# --- Selenium imports ---
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException, WebDriverException

# --- Google GenAI SDK imports ---
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

# --- Importaciones de módulos locales del proyecto ---
try:
    import database
except ImportError:
    database = None

try:
    import exportar_csv
except ImportError:
    exportar_csv = None

# ==============================================================================
# CONFIGURACIÓN Y CONSTANTES DEL SISTEMA (v1.3)
# ==============================================================================

VERSION_SCRIPT = "1.3"
RUC_EMPRESA_TARGET = "20601014193"
NOMBRE_CHAT_TARGET = "Facturas ponderosa"

DIR_BASE = Path(__file__).parent.resolve()
DIR_SESION_WA = DIR_BASE / "sesion_whatsapp"
DIR_COMPROBANTES = DIR_BASE / "comprobantes_whatsapp"
DIR_PROCESADOS = DIR_COMPROBANTES / "procesados"
DIR_ERRORES = DIR_COMPROBANTES / "errores"
FILE_LOG_ERRORES = DIR_COMPROBANTES / "errores_procesamiento.log"
FILE_CSV_OUT = DIR_BASE / "comprobantes_ponderosa.csv"

# Creación de estructura de directorios
for d in [DIR_SESION_WA, DIR_COMPROBANTES, DIR_PROCESADOS, DIR_ERRORES]:
    d.mkdir(parents=True, exist_ok=True)

# Configuración de Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(DIR_BASE / "agente_ponderosa.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("AgentePonderosa")

# ==============================================================================
# ESTRUCTURA DE DATOS EXTRAÍDOS (PYDANTIC / GEMINI STRUCTURED OUTPUT)
# ==============================================================================

class ComprobanteSchema(BaseModel):
    tipo_documento: str = Field(
        description="Tipo de documento contable: FACTURA, BOLETA, VALE_COMBUSTIBLE, LIQUIDACION, NOTA_CREDITO u OTRO"
    )
    serie_numero: str = Field(default="", description="Serie y número del documento (ej. F001-00012345 o 00123)")
    ruc_emisor: str = Field(default="", description="RUC del emisor del comprobante (11 dígitos)")
    razon_social: str = Field(default="", description="Nombre o Razón Social del emisor")
    ruc_receptor: str = Field(default="", description="RUC del receptor/cliente (ej. 20601014193)")
    fecha_emision: str = Field(default="", description="Fecha de emisión en formato YYYY-MM-DD")
    hora: str = Field(default="", description="Hora de emisión en formato HH:MM:SS (si aplica)")
    galones: float = Field(default=0.0, description="Cantidad de galones de combustible (si aplica)")
    concepto: str = Field(default="", description="Descripción corta del servicio o producto adquirido")
    chofer: str = Field(default="", description="Nombre del chofer o persona que firma/autoriza el vale")
    moneda: str = Field(default="PEN", description="Moneda del comprobante: PEN o USD")
    op_gravada: float = Field(default=0.0, description="Monto de la operación gravada / subtotal sin IGV")
    igv: float = Field(default=0.0, description="Monto del IGV (18%)")
    total: float = Field(default=0.0, description="Monto total del comprobante")
    placa: str = Field(default="", description="Número de placa del vehículo asociado (ej. ABC-123)")

# ==============================================================================
# UTILIDADES AUXILIARES
# ==============================================================================

def purgar_procesos_chrome():
    """Elimina procesos huérfanos de Chrome para evitar bloqueos del perfil de usuario."""
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/IM", "chromedriver.exe", "/T"], stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
            subprocess.run(["taskkill", "/F", "/IM", "chrome.exe", "/T"], stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
    except Exception:
        pass

def calcular_sha256(filepath: Path) -> str:
    """Calcula la firma hash SHA-256 de un archivo para evitar procesar duplicados."""
    sha256 = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(8192):
            sha256.update(chunk)
    return sha256.hexdigest()

def registrar_error_archivo(filepath: Path, razon: str):
    """Mueve el archivo fallido o no contable a comprobantes_whatsapp/errores."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    mensaje = f"[{timestamp}] ARCHIVO: {filepath.name} | RAZÓN: {razon}\n"
    
    with open(FILE_LOG_ERRORES, "a", encoding="utf-8") as f:
        f.write(mensaje)
    
    destino = DIR_ERRORES / filepath.name
    if destino.exists():
        destino.unlink()
    shutil.move(str(filepath), str(destino))
    logger.warning(f"Archivo trasladado a errores: {filepath.name} ({razon})")

# ==============================================================================
# MÓDULO 1: SELENIUM (WhatsApp Web v1.3 - Apertura de Chat Resiliente)
# ==============================================================================

class WhatsAppWebScraper:
    def __init__(self, session_path: Path):
        self.session_path = session_path
        self.driver: Optional[webdriver.Chrome] = None

    def inicializar_driver(self):
        """Abre Chrome utilizando la carpeta de sesión persistente."""
        purgar_procesos_chrome()
        
        lock_file = self.session_path / "SingletonLock"
        if lock_file.exists():
            try:
                lock_file.unlink()
            except Exception:
                pass

        options = Options()
        options.add_argument(f"--user-data-dir={self.session_path}")
        options.add_argument("--profile-directory=Default")
        options.add_argument("--start-maximized")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option('useAutomationExtension', False)
        
        prefs = {
            "download.default_directory": str(DIR_COMPROBANTES),
            "download.prompt_for_download": False,
            "plugins.always_open_pdf_externally": True,
            "download.directory_upgrade": True
        }
        options.add_experimental_option("prefs", prefs)

        logger.info(f"Inicializando Chrome Driver (v{VERSION_SCRIPT})...")
        self.driver = webdriver.Chrome(options=options)
        self.driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

    def abrir_whatsapp(self, timeout: int = 60):
        """Abre la interfaz web de WhatsApp."""
        self.driver.get("https://web.whatsapp.com")
        logger.info("Esperando carga de WhatsApp Web...")
        
        WebDriverWait(self.driver, timeout).until(
            EC.presence_of_element_located((By.XPATH, '//div[@id="side"] | //div[@role="grid"] | //body'))
        )
        time.sleep(5)  # v1.3: Pausa extendida para renderizado total del DOM
        logger.info("WhatsApp Web listo para operar.")

    def abrir_chat_target(self, nombre_chat: str) -> bool:
        """Abre la conversación indicada aplicando 4 estrategias de rescate (v1.3)."""
        # Estrategia 1: Clic directo si el chat está visible o fijado
        try:
            elementos = self.driver.find_elements(By.XPATH, f'//*[contains(@title, "{nombre_chat}")] | //*[text()="{nombre_chat}"]')
            for el in elementos:
                if el.is_displayed():
                    el.click()
                    logger.info(f"Chat '{nombre_chat}' seleccionado directamente en la interfaz.")
                    time.sleep(2)
                    return True
        except Exception:
            pass

        # Estrategia 2: Búsqueda por inspección de DOM con JavaScript
        logger.info(f"Buscando el chat '{nombre_chat}' mediante inspección JS...")
        try:
            res = self.driver.execute_script(f"""
                var targetName = "{nombre_chat}";
                var elems = document.querySelectorAll('span[title], div[title], span');
                for (var i = 0; i < elems.length; i++) {{
                    if (elems[i].innerText && elems[i].innerText.includes(targetName)) {{
                        elems[i].click();
                        return true;
                    }}
                }}
                return false;
            """)
            if res:
                logger.info(f"Chat '{nombre_chat}' abierto mediante JavaScript.")
                time.sleep(2)
                return True
        except Exception as e_js:
            logger.debug(f"Intento JS fallido: {e_js}")

        # Estrategia 3: Atajo de teclado nativo (Ctrl + Alt + /)
        try:
            body = self.driver.find_element(By.TAG_NAME, "body")
            actions = ActionChains(self.driver)
            actions.key_down(Keys.CONTROL).key_down(Keys.ALT).send_keys('/').key_up(Keys.ALT).key_up(Keys.CONTROL).perform()
            time.sleep(1.0)

            active_elem = self.driver.switch_to.active_element
            active_elem.send_keys(nombre_chat)
            time.sleep(1.5)
            active_elem.send_keys(Keys.ENTER)
            time.sleep(2.0)

            headers = self.driver.find_elements(By.XPATH, f'//header//*[contains(text(), "{nombre_chat}")]')
            if headers:
                logger.info(f"Chat '{nombre_chat}' abierto mediante teclado nativo.")
                return True
        except Exception as e_kb:
            logger.debug(f"Intento por teclado fallido: {e_kb}")

        logger.error(f"No se pudo encontrar o abrir el chat '{nombre_chat}'. Verifique que esté visible en WhatsApp.")
        return False

    def scroll_historial(self, iteraciones: int = 8):
        """Scroll vertical para cargar mensajes, imágenes y PDFs del historial."""
        try:
            chat_pane_selectores = [
                '//div[@id="main"]//div[contains(@class, "copyable-area")]//div[@tabindex="0"]',
                '//div[@id="main"]//div[@role="application"]',
                '//div[@id="main"]//div[contains(@class, "message-in")]/parent::div'
            ]
            
            chat_pane = None
            for sel in chat_pane_selectores:
                elems = self.driver.find_elements(By.XPATH, sel)
                for el in elems:
                    if el.is_displayed():
                        chat_pane = el
                        break
                if chat_pane:
                    break

            if chat_pane:
                for i in range(iteraciones):
                    self.driver.execute_script("""
                        arguments[0].scrollTop = 0;
                        arguments[0].dispatchEvent(new Event('scroll', { bubbles: true }));
                    """, chat_pane)
                    time.sleep(2.0)
                logger.info(f"Scroll de renderizado completado ({iteraciones} iteraciones).")
        except Exception as e:
            logger.warning(f"Excepción al desplazar el historial: {e}")

    def descargar_adjuntos_chat(self) -> List[Path]:
        """Descarga imágenes Blob y activa la descarga de archivos PDF adjuntos."""
        descargados = []

        # 1. Obtener imágenes Blob
        blob_imgs = self.driver.find_elements(By.XPATH, '//div[@id="main"]//img[starts-with(@src, "blob:")]')
        logger.info(f"Imágenes blob detectadas: {len(blob_imgs)}")

        for idx, img in enumerate(blob_imgs):
            try:
                src_blob = img.get_attribute("src")
                if not src_blob:
                    continue

                base64_data = self.driver.execute_async_script("""
                    var uri = arguments[0];
                    var callback = arguments[1];
                    var xhr = new XMLHttpRequest();
                    xhr.responseType = 'blob';
                    xhr.onload = function() {
                        var reader = new FileReader();
                        reader.onloadend = function() {
                            callback(reader.result.split(',')[1]);
                        };
                        reader.readAsDataURL(xhr.response);
                    };
                    xhr.open('GET', uri);
                    xhr.send();
                """, src_blob)

                if base64_data:
                    file_bytes = base64.b64decode(base64_data)
                    file_hash = hashlib.sha256(file_bytes).hexdigest()[:12]
                    filename = f"wa_img_{int(time.time())}_{file_hash}.jpg"
                    filepath = DIR_COMPROBANTES / filename

                    if not filepath.exists():
                        with open(filepath, "wb") as f:
                            f.write(file_bytes)
                        descargados.append(filepath)
                        logger.info(f"Imagen descargada: {filepath.name}")
            except Exception as e:
                logger.error(f"Error procesando imagen blob #{idx}: {e}")

        # 2. Descargar archivos PDF adjuntos (v1.3)
        selectores_pdf = [
            '//div[@id="main"]//div[contains(@title, ".pdf") or contains(@title, ".PDF")]',
            '//div[@id="main"]//span[contains(text(), ".pdf") or contains(text(), ".PDF")]/ancestor::div[@role="button"]',
            '//div[@id="main"]//span[@data-icon="download"]/ancestor::div[@role="button"]',
            '//div[@id="main"]//span[@data-icon="document-pdf"]/ancestor::div[@role="button"]'
        ]

        elementos_pdf = []
        for selector in selectores_pdf:
            found = self.driver.find_elements(By.XPATH, selector)
            for item in found:
                if item not in elementos_pdf:
                    elementos_pdf.append(item)

        logger.info(f"Documentos PDF / Botones detectados: {len(elementos_pdf)}")

        for idx, doc_elem in enumerate(elementos_pdf):
            try:
                self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", doc_elem)
                time.sleep(0.8)

                botones_descarga = doc_elem.find_elements(By.XPATH, './/span[@data-icon="download"]')
                if botones_descarga:
                    botones_descarga[0].click()
                else:
                    doc_elem.click()

                time.sleep(3.0)
            except Exception as e_pdf:
                logger.debug(f"Click omitido en PDF #{idx + 1}: {e_pdf}")

        for file in DIR_COMPROBANTES.glob("*"):
            if file.is_file() and file.suffix.lower() in [".jpg", ".jpeg", ".png", ".pdf", ".webp"]:
                if file not in descargados and file.parent == DIR_COMPROBANTES:
                    descargados.append(file)

        return descargados

    def cerrar(self):
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
        purgar_procesos_chrome()

# ==============================================================================
# MÓDULO 2: OCR Y PROCESAMIENTO MULTIMODAL CON GEMINI AI
# ==============================================================================

class AuditoriaContableGemini:
    def __init__(self):
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            logger.warning("GEMINI_API_KEY no se encuentra definida en el entorno.")
        
        self.client = genai.Client()
        self.model_id = "gemini-3.8-flash"

    def audit_document(self, filepath: Path) -> ComprobanteSchema:
        logger.info(f"Enviando archivo a Gemini OCR ({self.model_id}): {filepath.name}")
        
        prompt = f"""
        Actúa como un Auditor Contable y Fiscal Experto de Perú.
        Analiza detalladamente este archivo (comprobante de pago, factura, boleta, nota de crédito o vale de combustible manuscrito).
        
        INSTRUCCIONES CLAVE:
        1. Identifica los datos del documento con precisión.
        2. Si es un vale manuscrito o ticket de combustible, extrae la cantidad de galones, la placa del vehículo, el nombre del chofer y el total.
        3. El RUC receptor o cliente principal a verificar es {RUC_EMPRESA_TARGET} (PONDEROSA MINERALS S.A.C.).
        4. Si el archivo es ilegible, no es contable (foto personal, meme) o no posee importes, asigna tipo_documento = "OTRO" y 0.0 a los valores numéricos.
        5. Normaliza la fecha a YYYY-MM-DD.
        6. Retorna el resultado completando la estructura JSON.
        """

        extension = filepath.suffix.lower()
        mime_types = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".webp": "image/webp",
            ".pdf": "application/pdf"
        }
        mime_type = mime_types.get(extension, "application/octet-stream")

        with open(filepath, "rb") as f:
            file_bytes = f.read()

        file_part = types.Part.from_bytes(
            data=file_bytes,
            mime_type=mime_type
        )

        response = self.client.models.generate_content(
            model=self.model_id,
            contents=[file_part, prompt],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=ComprobanteSchema,
                temperature=0.1,
            ),
        )

        data_json = json.loads(response.text)
        return ComprobanteSchema(**data_json)

# ==============================================================================
# MÓDULO 3: INTEGRACIÓN Y WORKFLOW AUTÓNOMO
# ==============================================================================

class AgentePonderosaManager:
    def __init__(self):
        self.auditor = AuditoriaContableGemini()

    def procesar_lote_comprobantes(self):
        archivos = [
            f for f in DIR_COMPROBANTES.glob("*") 
            if f.is_file() and f.suffix.lower() in [".jpg", ".jpeg", ".png", ".pdf", ".webp"]
        ]
        
        if not archivos:
            logger.info("No hay archivos pendientes de procesamiento en la carpeta de comprobantes.")
            return

        logger.info(f"Archivos encontrados para procesar: {len(archivos)}")

        for filepath in archivos:
            try:
                file_hash = calcular_sha256(filepath)
                
                if database and hasattr(database, "existe_hash_en_db"):
                    if database.existe_hash_en_db(file_hash):
                        registrar_error_archivo(filepath, f"Archivo duplicado (SHA256): {file_hash}")
                        continue

                try:
                    resultado: ComprobanteSchema = self.auditor.audit_document(filepath)
                except Exception as e_ocr:
                    registrar_error_archivo(filepath, f"Error en la API de Gemini: {str(e_ocr)}")
                    continue

                if (resultado.tipo_documento.upper() == "OTRO" or 
                    (resultado.total <= 0.0 and resultado.galones <= 0.0)):
                    registrar_error_archivo(
                        filepath, 
                        f"Documento no contable o en cero (tipo: {resultado.tipo_documento}, total: {resultado.total})"
                    )
                    continue

                registro_dict = resultado.model_dump()
                registro_dict["hash_archivo"] = file_hash
                registro_dict["nombre_archivo"] = filepath.name

                if database and hasattr(database, "insertar_comprobante"):
                    try:
                        database.insertar_comprobante(registro_dict)
                        logger.info(f"Grabado en la base de datos: {resultado.serie_numero}")
                    except Exception as e_db:
                        logger.error(f"Error registrando en DB para {filepath.name}: {e_db}")

                if exportar_csv and hasattr(exportar_csv, "agregar_o_actualizar_csv"):
                    try:
                        exportar_csv.agregar_o_actualizar_csv(registro_dict, FILE_CSV_OUT)
                    except Exception as e_csv:
                        logger.error(f"Error al escribir CSV: {e_csv}")

                destino_exito = DIR_PROCESADOS / filepath.name
                if destino_exito.exists():
                    destino_exito.unlink()
                shutil.move(str(filepath), str(destino_exito))
                logger.info(f"Procesado con éxito: {filepath.name} -> procesados/")

            except Exception as e_general:
                registrar_error_archivo(filepath, f"Excepción imprevista: {str(e_general)}")

    def ejecutar_ciclo_completo(self):
        logger.info("==================================================================")
        logger.info(f"  INICIANDO AGENTE AUTÓNOMO PONDEROSA MINERALS S.A.C. (v{VERSION_SCRIPT})")
        logger.info("==================================================================")

        scraper = WhatsAppWebScraper(DIR_SESION_WA)
        try:
            scraper.inicializar_driver()
            scraper.abrir_whatsapp()
            
            if scraper.abrir_chat_target(NOMBRE_CHAT_TARGET):
                scraper.scroll_historial(iteraciones=6)
                descargados = scraper.descargar_adjuntos_chat()
                logger.info(f"Archivos descargados/encontrados: {len(descargados)}")
            else:
                logger.warning("No se interactuó con el chat. Se revisarán archivos locales.")
        
        except Exception as e_wa:
            logger.error(f"Excepción en Selenium/WhatsApp: {e_wa}")
        finally:
            scraper.cerrar()

        self.procesar_lote_comprobantes()
        logger.info(f"Ejecución finalizada (v{VERSION_SCRIPT}).")

# ==============================================================================
# PUNTO DE ENTRADA
# ==============================================================================

if __name__ == "__main__":
    agente = AgentePonderosaManager()
    agente.ejecutar_ciclo_completo()
