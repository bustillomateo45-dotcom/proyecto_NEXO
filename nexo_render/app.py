import sys
import subprocess
import importlib
import os
import hashlib
import threading
import webbrowser
import traceback

# En Windows la consola puede usar cp1252/cp850: sin esto, imprimir tildes o
# símbolos puede lanzar UnicodeEncodeError. Con esto nunca rompe el programa.
for _flujo in (sys.stdout, sys.stderr):
    try:
        _flujo.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# =========================================================
# INSTALAR DEPENDENCIAS
# =========================================================

def _asegurar_libreria(nombre_paquete, nombre_import=None):

    nombre_import = nombre_import or nombre_paquete

    try:

        importlib.import_module(nombre_import)

    except ImportError:

        # En Render las dependencias vienen de requirements.txt
        if os.environ.get("RENDER") or os.environ.get("NEXO_NO_AUTOINSTALL"):
            raise

        print(
            f"[Instalando dependencia: {nombre_paquete}]"
        )

        subprocess.check_call([
            sys.executable,
            "-m",
            "pip",
            "install",
            nombre_paquete
        ])

        print(
            f"[{nombre_paquete} instalado correctamente]"
        )


# =========================================================
# DEPENDENCIAS
# =========================================================

_asegurar_libreria(
    "flask",
    "flask"
)

_asegurar_libreria(
    "psycopg2-binary",
    "psycopg2"
)


from flask import (
    Flask,
    render_template,
    request,
    jsonify,
    session,
    redirect,
    url_for,
    flash
)

import psycopg2
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager


# =========================================================
# CONFIGURACIÓN FLASK
# =========================================================

BASE_DIR = os.path.dirname(
    os.path.abspath(__file__)
)


app = Flask(
    __name__,
    template_folder=os.path.join(BASE_DIR, "templates"),
    static_folder=os.path.join(BASE_DIR, "static")
)


# =========================================================
# CARGADOR DE PLANTILLAS TOLERANTE
# ---------------------------------------------------------
# El código pide "estudiantes/dashboard.html", etc., pero los
# archivos pueden estar como "menu_estudiantes.html", sueltos
# junto a app.py o dentro de la carpeta templates/. Este
# cargador busca en ambos sitios y con ambos nombres, así el
# error TemplateNotFound no vuelve a pasar en ningún menú.
# =========================================================

from jinja2 import BaseLoader, FileSystemLoader, TemplateNotFound

ALIAS_PLANTILLAS = {
    "estudiantes/dashboard.html": "menu_estudiantes.html",
    "profesores/dashboard.html": "menu_profesor.html",
    "coordinadores/dashboard.html": "menu_coordinadores.html",
    "administracion/dashboard.html": "menu_administracion.html",
    "administracion/gestion.html": "gestion.html",
}


class _CargadorNexo(BaseLoader):

    def __init__(self, carpetas):

        self._cargadores = [FileSystemLoader(c) for c in carpetas]

    def get_source(self, environment, template):

        candidatos = [template]

        if template in ALIAS_PLANTILLAS:
            candidatos.append(ALIAS_PLANTILLAS[template])

        # también acepta el nombre sin subcarpeta (ej. "dashboard.html" no, pero
        # "estudiantes/menu_estudiantes.html" sí cae en el nombre base)
        candidatos.append(os.path.basename(template))

        for nombre in candidatos:

            for cargador in self._cargadores:

                try:
                    return cargador.get_source(environment, nombre)
                except TemplateNotFound:
                    continue

        raise TemplateNotFound(template)


app.jinja_loader = _CargadorNexo([
    os.path.join(BASE_DIR, "templates"),
    BASE_DIR,
])


# =====================================================
# CAMBIA ESTO por una clave secreta real y única.
# Se usa para firmar la cookie de sesión.
# =====================================================

# En Render la variable RENDER="true" existe siempre.
EN_PRODUCCION = bool(os.environ.get("RENDER"))

app.secret_key = os.environ.get("SECRET_KEY") or "clave-solo-para-desarrollo-local"

if EN_PRODUCCION and not os.environ.get("SECRET_KEY"):
    raise RuntimeError(
        "Falta la variable de entorno SECRET_KEY en Render "
        "(Environment > Add Environment Variable)."
    )

# Render pone un proxy HTTPS delante: esto hace que Flask vea https y la IP real
from werkzeug.middleware.proxy_fix import ProxyFix

app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# La sesión se GUARDA: no hay que volver a iniciar sesión al cerrar el
# navegador ni al reiniciar el servidor (mientras no cambies la clave de arriba).
# Se renueva sola cada vez que usas el programa.
app.permanent_session_lifetime = timedelta(days=30)

app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SECURE"] = EN_PRODUCCION   # solo por HTTPS en Render


# =========================================================
# CONFIGURACIÓN POSTGRESQL
# =========================================================

# Puedes cambiar los valores aquí o, sin tocar el código, con variables de
# entorno antes de ejecutar (en cmd):
#     set NEXO_DB_PASSWORD=mi_clave_real
#     py app.py

DB_CONFIG = {

    "host": os.environ.get("NEXO_DB_HOST", "localhost"),

    "port": int(os.environ.get("NEXO_DB_PORT", "5432")),

    "dbname": os.environ.get("NEXO_DB_NAME", "nexo"),

    "user": os.environ.get("NEXO_DB_USER", "postgres"),

    # =====================================================
    # CAMBIA ESTO POR LA CONTRASEÑA REAL DE POSTGRESQL
    # =====================================================

    "password": os.environ.get("NEXO_DB_PASSWORD", "123456")
}

# En Render, la base de datos entrega una sola URL: DATABASE_URL
DATABASE_URL = os.environ.get("DATABASE_URL")


# =========================================================
# ROLES VÁLIDOS
# =========================================================

ROLES_VALIDOS = ["estudiante", "profesor", "coordinador", "administracion"]

# A qué página se va cada rol después de iniciar sesión
RUTA_POR_ROL = {
    "estudiante": "/estudiante",
    "profesor": "/profesor",
    "coordinador": "/coordinador",
    "administracion": "/administracion",
}


# =========================================================
# CONEXIÓN A POSTGRESQL
# =========================================================

def _texto_error_bytes(error):
    """Recupera el mensaje REAL de PostgreSQL cuando psycopg2 no puede leerlo.

    En Windows en español, el servidor responde los errores de conexión
    (contraseña incorrecta, base inexistente...) en cp1252 con tildes, y
    psycopg2 los intenta leer como UTF-8 -> UnicodeDecodeError. Los bytes
    originales vienen dentro de la excepción, así que se decodifican bien.
    """

    crudo = getattr(error, "object", b"")

    if isinstance(crudo, (bytes, bytearray)):

        for codificacion in ("utf-8", "cp1252", "latin-1"):

            try:
                return bytes(crudo).decode(codificacion).strip()
            except UnicodeDecodeError:
                continue

    return "PostgreSQL devolvió un mensaje de error que no se pudo leer."


def _conectar_db():

    if DATABASE_URL:

        return psycopg2.connect(DATABASE_URL, connect_timeout=10)

    try:

        return psycopg2.connect(

            host=DB_CONFIG["host"],

            port=DB_CONFIG["port"],

            dbname=DB_CONFIG["dbname"],

            user=DB_CONFIG["user"],

            password=DB_CONFIG["password"],

            connect_timeout=5
        )

    except UnicodeDecodeError as e:

        # se convierte en un error de conexión normal, que ya manejan
        # todas las rutas (psycopg2.Error) con el mensaje real
        raise psycopg2.OperationalError(_texto_error_bytes(e)) from None


# =========================================================
# CREAR / COMPROBAR ESQUEMA COMPLETO DE NEXO (9 tablas)
# =========================================================

_SENTENCIAS_ESQUEMA = [

    # 1. GRADOS
    """
    CREATE TABLE IF NOT EXISTS grados (
        id      SERIAL PRIMARY KEY,
        nombre  VARCHAR(50) NOT NULL UNIQUE
    )
    """,

    # 2. MATERIAS
    """
    CREATE TABLE IF NOT EXISTS materias (
        id      SERIAL PRIMARY KEY,
        nombre  VARCHAR(100) NOT NULL UNIQUE
    )
    """,

    # 3. USUARIOS
    """
    CREATE TABLE IF NOT EXISTS usuarios (
        id               SERIAL PRIMARY KEY,
        nombre           VARCHAR(100) NOT NULL,
        usuario          VARCHAR(50) NOT NULL UNIQUE,
        contrasena_hash  VARCHAR(255) NOT NULL,
        rol              VARCHAR(20) NOT NULL
            CHECK (rol IN ('estudiante', 'profesor', 'coordinador', 'administracion'))
    )
    """,

    # 4. GRUPOS (ej: "11-06", ligado a un grado)
    """
    CREATE TABLE IF NOT EXISTS grupos (
        id        SERIAL PRIMARY KEY,
        grado_id  INTEGER NOT NULL REFERENCES grados(id) ON DELETE RESTRICT,
        nombre    VARCHAR(20) NOT NULL,
        UNIQUE (grado_id, nombre)
    )
    """,

    # 5. ESTUDIANTE_GRUPO (cada estudiante pertenece a UN grupo)
    """
    CREATE TABLE IF NOT EXISTS estudiante_grupo (
        id             SERIAL PRIMARY KEY,
        estudiante_id  INTEGER NOT NULL UNIQUE REFERENCES usuarios(id) ON DELETE CASCADE,
        grupo_id       INTEGER NOT NULL REFERENCES grupos(id) ON DELETE RESTRICT
    )
    """,

    # 6. PROFESOR_MATERIA_GRUPO (un profesor: varias materias, varios grupos)
    """
    CREATE TABLE IF NOT EXISTS profesor_materia_grupo (
        id           SERIAL PRIMARY KEY,
        profesor_id  INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
        materia_id   INTEGER NOT NULL REFERENCES materias(id) ON DELETE RESTRICT,
        grupo_id     INTEGER NOT NULL REFERENCES grupos(id) ON DELETE RESTRICT,
        UNIQUE (profesor_id, materia_id, grupo_id)
    )
    """,

    # 7. COORDINADOR_AREA
    """
    CREATE TABLE IF NOT EXISTS coordinador_area (
        id              SERIAL PRIMARY KEY,
        coordinador_id  INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
        materia_id      INTEGER NOT NULL REFERENCES materias(id) ON DELETE RESTRICT,
        grado_id        INTEGER REFERENCES grados(id) ON DELETE RESTRICT
    )
    """,

    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_coordinador_area_con_grado
        ON coordinador_area (coordinador_id, materia_id, grado_id)
        WHERE grado_id IS NOT NULL
    """,

    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_coordinador_area_sin_grado
        ON coordinador_area (coordinador_id, materia_id)
        WHERE grado_id IS NULL
    """,

    # 8. CANALES
    #    'grupo_materia' -> chat de una materia para un grupo puntual
    #    'coordinacion'  -> anuncios de coordinación para un grado
    #    'general'       -> aviso para toda la institución
    #    'directo'       -> chat de administración con un coordinador/profesor
    """
    CREATE TABLE IF NOT EXISTS canales (
        id          SERIAL PRIMARY KEY,
        tipo        VARCHAR(30) NOT NULL,
        grupo_id    INTEGER REFERENCES grupos(id) ON DELETE RESTRICT,
        materia_id  INTEGER REFERENCES materias(id) ON DELETE RESTRICT,
        grado_id    INTEGER REFERENCES grados(id) ON DELETE RESTRICT,
        usuario_id  INTEGER REFERENCES usuarios(id) ON DELETE CASCADE
    )
    """,

    # --- migración para bases creadas con el esquema anterior ---
    """
    ALTER TABLE canales
        ADD COLUMN IF NOT EXISTS usuario_id INTEGER
        REFERENCES usuarios(id) ON DELETE CASCADE
    """,
    "ALTER TABLE canales DROP CONSTRAINT IF EXISTS canales_tipo_check",
    "ALTER TABLE canales DROP CONSTRAINT IF EXISTS chk_canal_coherente",
    """
    ALTER TABLE canales ADD CONSTRAINT canales_tipo_check
        CHECK (tipo IN ('grupo_materia', 'coordinacion', 'general', 'directo'))
    """,
    """
    ALTER TABLE canales ADD CONSTRAINT chk_canal_coherente CHECK (
        (tipo = 'grupo_materia' AND grupo_id IS NOT NULL
                                 AND materia_id IS NOT NULL
                                 AND grado_id IS NULL
                                 AND usuario_id IS NULL)
        OR
        (tipo = 'coordinacion' AND grado_id IS NOT NULL
                                AND grupo_id IS NULL
                                AND usuario_id IS NULL)
        OR
        (tipo = 'general' AND grupo_id IS NULL AND materia_id IS NULL
                          AND grado_id IS NULL AND usuario_id IS NULL)
        OR
        (tipo = 'directo' AND usuario_id IS NOT NULL AND grupo_id IS NULL
                          AND materia_id IS NULL AND grado_id IS NULL)
    )
    """,

    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_canal_grupo_materia
        ON canales (grupo_id, materia_id)
        WHERE grupo_id IS NOT NULL
    """,

    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_canal_coordinacion
        ON canales (grado_id, COALESCE(materia_id, 0))
        WHERE grado_id IS NOT NULL AND grupo_id IS NULL
    """,

    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_canal_general
        ON canales (tipo)
        WHERE tipo = 'general'
    """,

    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_canal_directo
        ON canales (usuario_id)
        WHERE tipo = 'directo'
    """,

    # 9. MENSAJES
    """
    CREATE TABLE IF NOT EXISTS mensajes (
        id         SERIAL PRIMARY KEY,
        canal_id   INTEGER NOT NULL REFERENCES canales(id) ON DELETE CASCADE,
        emisor_id  INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE RESTRICT,
        contenido  TEXT NOT NULL,
        timestamp  TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,

    """
    CREATE INDEX IF NOT EXISTS ix_mensajes_canal_fecha
        ON mensajes (canal_id, timestamp)
    """,

    # --- archivos adjuntos (fotos y documentos) ---
    "ALTER TABLE mensajes ADD COLUMN IF NOT EXISTS archivo_nombre VARCHAR(255)",
    "ALTER TABLE mensajes ADD COLUMN IF NOT EXISTS archivo_clave  VARCHAR(80)",
    "ALTER TABLE mensajes ADD COLUMN IF NOT EXISTS archivo_tipo   VARCHAR(20)",
    "ALTER TABLE mensajes ADD COLUMN IF NOT EXISTS archivo_tam    INTEGER",

    # --- mensajes reenviados: guarda el autor original ---
    "ALTER TABLE mensajes ADD COLUMN IF NOT EXISTS reenviado_de VARCHAR(160)",

    # --- notificaciones push (llegan aunque la página esté cerrada) ---
    """
    CREATE TABLE IF NOT EXISTS push_suscripciones (
        id          SERIAL PRIMARY KEY,
        usuario_id  INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
        endpoint    TEXT    NOT NULL UNIQUE,
        p256dh      TEXT    NOT NULL,
        auth        TEXT    NOT NULL,
        creada      TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
]


# ---------------------------------------------------------
# DATOS BASE (se insertan solo si no existen)
# ---------------------------------------------------------

GRADOS_BASE = [str(n) for n in range(1, 12)]   # grados 1 a 11

MATERIAS_BASE = [
    "Matemáticas", "Lengua Castellana", "Inglés", "Ciencias Naturales",
    "Física", "Química", "Ciencias Sociales", "Educación Física",
    "Educación Artística", "Ética y Valores", "Religión",
    "Tecnología e Informática", "Filosofía",
]

# Siglas que se ven en el avatar de cada materia
AVATAR_MATERIA = {
    "Matemáticas": "MA", "Lengua Castellana": "LC", "Inglés": "IN",
    "Ciencias Naturales": "CN", "Física": "FI", "Química": "QU",
    "Ciencias Sociales": "CS", "Educación Física": "EF",
    "Educación Artística": "EA", "Ética y Valores": "EV",
    "Religión": "RE", "Tecnología e Informática": "TI", "Filosofía": "FL",
}


def _sembrar_datos_base(cursor):

    for grado in GRADOS_BASE:
        cursor.execute(
            "INSERT INTO grados (nombre) VALUES (%s) ON CONFLICT DO NOTHING",
            (grado,)
        )

    for materia in MATERIAS_BASE:
        cursor.execute(
            "INSERT INTO materias (nombre) VALUES (%s) ON CONFLICT DO NOTHING",
            (materia,)
        )

    # canal general (toda la institución)
    cursor.execute(
        "INSERT INTO canales (tipo) VALUES ('general') ON CONFLICT DO NOTHING"
    )

    # un canal de coordinación por cada grado
    cursor.execute(
        """
        INSERT INTO canales (tipo, grado_id)
        SELECT 'coordinacion', g.id
        FROM grados g
        WHERE NOT EXISTS (
            SELECT 1 FROM canales c
            WHERE c.tipo = 'coordinacion'
              AND c.grado_id = g.id
              AND c.materia_id IS NULL
        )
        ON CONFLICT DO NOTHING
        """
    )

    # administrador inicial, solo si no existe ninguno
    cursor.execute("SELECT 1 FROM usuarios WHERE rol = 'administracion' LIMIT 1")

    if cursor.fetchone() is None:

        usuario_admin = os.environ.get("NEXO_ADMIN_USER", "admin")

        clave_admin = os.environ.get("NEXO_ADMIN_PASS")

        clave_generada = False

        if not clave_admin:

            clave_generada = True

            if EN_PRODUCCION:
                import secrets
                clave_admin = secrets.token_urlsafe(12)
            else:
                clave_admin = "admin123"

        cursor.execute(
            """
            INSERT INTO usuarios (nombre, usuario, contrasena_hash, rol)
            VALUES (%s, %s, %s, 'administracion')
            ON CONFLICT DO NOTHING
            """,
            ("Administración", usuario_admin, _hash_password(clave_admin))
        )

        if cursor.rowcount:
            print("========================================")
            print(" ADMINISTRADOR INICIAL CREADO")
            print("   usuario   :", usuario_admin)
            print("   contraseña:", clave_admin if clave_generada else "(la de NEXO_ADMIN_PASS)")
            print(" Cámbiala desde Administración > Gestión.")
            print("========================================")
        else:
            print("AVISO: el usuario '%s' ya existe con otro rol; "
                  "no se creó el administrador inicial." % usuario_admin)


def _inicializar_db():

    conn = None
    cursor = None

    try:

        conn = _conectar_db()

        cursor = conn.cursor()

        # candado: si arrancan dos procesos, el segundo espera al primero
        cursor.execute("SELECT pg_advisory_xact_lock(727272)")

        for sentencia in _SENTENCIAS_ESQUEMA:
            cursor.execute(sentencia)

        _sembrar_datos_base(cursor)

        conn.commit()

        print("----------------------------------------")
        print("ESQUEMA DE NEXO VERIFICADO/CREADO (9 tablas)")
        print("----------------------------------------")


    except psycopg2.Error as e:

        if conn:
            conn.rollback()

        print("----------------------------------------")
        print("ERROR CREANDO EL ESQUEMA DE NEXO")
        print("----------------------------------------")
        print(e)
        print("----------------------------------------")

        raise


    finally:

        if cursor:
            cursor.close()

        if conn:
            conn.close()


# =========================================================
# HASH DE CONTRASEÑA ("salt_hex:hash_hex" en una sola columna)
# =========================================================

def _hash_password(password, salt=None):

    if salt is None:

        salt = os.urandom(16)
     #identificacion de cifrado de incriptacion 
    hash_bytes = hashlib.pbkdf2_hmac(

        "sha256",

        password.encode("utf-8"),

        salt,

        100_000
    )


    return f"{salt.hex()}:{hash_bytes.hex()}"


def _verificar_password(password, contrasena_guardada):

    try:

        salt_hex, hash_guardado = contrasena_guardada.split(
            ":",
            1
        )

    except (ValueError, AttributeError):

        return False


    intento = _hash_password(
        password,
        bytes.fromhex(salt_hex)
    )

    _, hash_intento = intento.split(":", 1)

    return hash_intento == hash_guardado


# =========================================================
# LÓGICA DE NEGOCIO
# (misma lógica de antes, ya no vive en una clase Api de
#  pywebview, sino en funciones normales que llaman las
#  rutas de Flask)
# =========================================================

def _registrar_usuario(nombre, usuario, password, rol=None):

    nombre = (nombre or "").strip()

    usuario = (usuario or "").strip().lower()

    password = password or ""

    rol = (rol or "estudiante").strip().lower()


    if not nombre:

        return {
            "ok": False,
            "error": "El nombre es obligatorio."
        }


    if len(nombre) < 3:

        return {
            "ok": False,
            "error": "El nombre debe tener al menos 3 caracteres."
        }


    if not usuario:

        return {
            "ok": False,
            "error": "El usuario (nombre de acceso) es obligatorio."
        }


    if len(usuario) < 3:

        return {
            "ok": False,
            "error": "El usuario debe tener al menos 3 caracteres."
        }


    if not password:

        return {
            "ok": False,
            "error": "La contraseña es obligatoria."
        }


    if len(password) < 4:

        return {
            "ok": False,
            "error": "La contraseña debe tener al menos 4 caracteres."
        }


    if rol not in ROLES_VALIDOS:

        rol = "estudiante"


    contrasena_hash = _hash_password(password)


    conn = None
    cursor = None


    try:

        conn = _conectar_db()

        cursor = conn.cursor()


        cursor.execute(
            """
            SELECT id
            FROM usuarios
            WHERE LOWER(usuario) = LOWER(%s)
            LIMIT 1
            """,
            (usuario,)
        )


        if cursor.fetchone():

            return {
                "ok": False,
                "error": "Ese usuario ya está registrado."
            }


        cursor.execute(
            """
            INSERT INTO usuarios
            (
                nombre,
                usuario,
                contrasena_hash,
                rol
            )
            VALUES
            (
                %s,
                %s,
                %s,
                %s
            )
            RETURNING id
            """,
            (
                nombre,
                usuario,
                contrasena_hash,
                rol
            )
        )


        id_usuario = cursor.fetchone()[0]

        conn.commit()


        print("----------------------------------------")
        print("USUARIO REGISTRADO")
        print("ID:", id_usuario)
        print("NOMBRE:", nombre)
        print("USUARIO:", usuario)
        print("----------------------------------------")


        return {

            "ok": True,

            "usuario_id": id_usuario,

            "nombre": nombre,

            "usuario": usuario,

            "rol": rol
        }


    except psycopg2.errors.UniqueViolation:

        if conn:
            conn.rollback()

        return {
            "ok": False,
            "error": "Ese usuario ya está registrado."
        }


    except psycopg2.Error as e:

        if conn:
            conn.rollback()

        print("ERROR POSTGRESQL AL REGISTRAR:", e)

        return {
            "ok": False,
            "error": "Error de PostgreSQL al registrar."
        }


    except Exception as e:

        if conn:
            conn.rollback()

        print("ERROR AL REGISTRAR:", e)

        return {
            "ok": False,
            "error": "Error al registrar el usuario."
        }


    finally:

        if cursor:
            cursor.close()

        if conn:
            conn.close()


def _iniciar_sesion(usuario_login, password):

    usuario_login = (usuario_login or "").strip()

    password = password or ""


    if not usuario_login or not password:

        return {

            "ok": False,

            "error": "Usuario y contraseña son obligatorios."
        }


    conn = None
    cursor = None


    try:

        conn = _conectar_db()

        cursor = conn.cursor()


        # -------------------------------------------------
        # "usuario" SÍ es UNIQUE en la tabla, así que este
        # match siempre identifica a una sola persona.
        # -------------------------------------------------

        cursor.execute(
            """
            SELECT
                id,
                nombre,
                contrasena_hash,
                rol
            FROM usuarios
            WHERE LOWER(usuario) = LOWER(%s)
            LIMIT 1
            """,
            (usuario_login,)
        )


        fila = cursor.fetchone()


        if not fila:

            return {
                "ok": False,
                "error": "Usuario o contraseña incorrectos."
            }


        usuario_id = fila[0]

        nombre = fila[1]

        contrasena_guardada = fila[2]

        rol = fila[3]


        if not _verificar_password(password, contrasena_guardada):

            return {
                "ok": False,
                "error": "Usuario o contraseña incorrectos."
            }


        print("----------------------------------------")
        print("LOGIN CORRECTO")
        print("NOMBRE:", nombre)
        print("ID:", usuario_id)
        print("----------------------------------------")


        return {

            "ok": True,

            "usuario_id": usuario_id,

            "nombre": nombre,

            "rol": rol
        }


    except psycopg2.Error as e:

        print("ERROR POSTGRESQL AL INICIAR SESIÓN:", e)

        return {
            "ok": False,
            "error": "No se pudo conectar con PostgreSQL."
        }


    except Exception as e:

        print("ERROR AL INICIAR SESIÓN:", e)

        return {
            "ok": False,
            "error": "Error al iniciar sesión."
        }


    finally:

        if cursor:
            cursor.close()

        if conn:
            conn.close()


# =========================================================
# CONTROL DE ACCESO POR ROL
# =========================================================

def _usuario_o_redirige_a_login():
    """Si no hay sesión activa, devuelve una redirección a /login.
    Si hay sesión, devuelve None (puede seguir)."""

    if not session.get("usuario_id"):
        return redirect(url_for("login"))

    return None


def _exige_rol(rol_esperado):
    """Verifica sesión + rol correcto para una ruta.
    Devuelve una redirección si hay que cortar, o None si puede seguir."""

    redireccion = _usuario_o_redirige_a_login()

    if redireccion:
        return redireccion

    rol_actual = session.get("rol")

    if rol_actual != rol_esperado:
        # lo manda a SU propia página en vez de a la que no le corresponde
        destino = RUTA_POR_ROL.get(rol_actual, "/login")
        return redirect(destino)

    return None


# =========================================================
# CONSULTAS DE APOYO PARA LAS VISTAS
# =========================================================

ZONA_COLOMBIA = timezone(timedelta(hours=-5))   # Colombia no usa horario de verano

PALABRAS_MENORES = {"y", "e", "de", "del", "la", "el", "los", "las"}


@contextmanager
def _cursor_db():
    """Abre conexión + cursor. Confirma al salir bien y revierte si hay error."""

    conn = _conectar_db()

    try:

        cursor = conn.cursor()

        yield cursor

        conn.commit()

    except Exception:

        conn.rollback()

        raise

    finally:

        conn.close()


# ---------- formato de horas y textos ----------

def _hora_lista(momento):

    local = momento.astimezone(ZONA_COLOMBIA)

    if local.date() == datetime.now(ZONA_COLOMBIA).date():
        return local.strftime("%H:%M")

    return local.strftime("%d/%m")


def _hora_burbuja(momento):

    local = momento.astimezone(ZONA_COLOMBIA)

    if local.date() == datetime.now(ZONA_COLOMBIA).date():
        return local.strftime("%H:%M")

    return local.strftime("%d/%m %H:%M")


def _iniciales(texto):

    palabras = [p for p in texto.split() if p.lower() not in PALABRAS_MENORES]

    if len(palabras) >= 2:
        return (palabras[0][0] + palabras[1][0]).upper()

    return texto[:2].upper()


def _avatar_materia(nombre):

    return AVATAR_MATERIA.get(nombre) or _iniciales(nombre)


def _nombre_grado(nombre):

    return "Grado " + nombre if nombre.isdigit() else nombre


def _orden_grado(nombre):

    # grados numéricos de mayor a menor (11, 10, ... 1); los demás al final
    if nombre.isdigit():
        return (0, -int(nombre))

    return (1, nombre)


# ---------- canales ----------

def _canal_grupo_materia(cursor, grupo_id, materia_id):
    """Devuelve el id del canal (grupo + materia); lo crea si no existe."""

    cursor.execute(
        """
        INSERT INTO canales (tipo, grupo_id, materia_id)
        VALUES ('grupo_materia', %s, %s)
        ON CONFLICT DO NOTHING
        """,
        (grupo_id, materia_id)
    )

    cursor.execute(
        """
        SELECT id FROM canales
        WHERE tipo = 'grupo_materia' AND grupo_id = %s AND materia_id = %s
        """,
        (grupo_id, materia_id)
    )

    return cursor.fetchone()[0]


def _canal_general(cursor):

    cursor.execute(
        "INSERT INTO canales (tipo) VALUES ('general') ON CONFLICT DO NOTHING"
    )

    cursor.execute("SELECT id FROM canales WHERE tipo = 'general'")

    return cursor.fetchone()[0]


def _canal_directo(cursor, usuario_id):
    """Chat entre administración y un coordinador/profesor."""

    cursor.execute(
        """
        INSERT INTO canales (tipo, usuario_id)
        VALUES ('directo', %s)
        ON CONFLICT DO NOTHING
        """,
        (usuario_id,)
    )

    cursor.execute(
        "SELECT id FROM canales WHERE tipo = 'directo' AND usuario_id = %s",
        (usuario_id,)
    )

    return cursor.fetchone()[0]


def _canales_de_grados(cursor):
    """Lista [(canal_id, nombre_del_grado)] de mayor a menor grado."""

    cursor.execute(
        """
        SELECT c.id, g.nombre
        FROM canales c
        JOIN grados g ON g.id = c.grado_id
        WHERE c.tipo = 'coordinacion' AND c.materia_id IS NULL
        """
    )

    filas = cursor.fetchall()

    filas.sort(key=lambda f: _orden_grado(f[1]))

    return filas


def _ultimos_mensajes(cursor, canal_ids):
    """Último mensaje de cada canal, para la vista previa de la lista."""

    canal_ids = [c for c in canal_ids if c is not None]

    if not canal_ids:
        return {}

    cursor.execute(
        """
        SELECT DISTINCT ON (m.canal_id)
               m.canal_id, m.contenido, m.timestamp, m.archivo_nombre
        FROM mensajes m
        WHERE m.canal_id = ANY(%s)
        ORDER BY m.canal_id, m.id DESC
        """,
        (canal_ids,)
    )

    resultado = {}

    for canal_id, contenido, momento, archivo_nombre in cursor.fetchall():

        texto = " ".join(contenido.split())

        if not texto and archivo_nombre:
            texto = "Archivo: " + archivo_nombre

        resultado[canal_id] = {
            "texto": texto[:80],
            "hora": _hora_lista(momento)
        }

    return resultado


def _item(canal_id, nombre, sub, avatar, ultimos, puede_escribir):

    ultimo = ultimos.get(canal_id)

    return {
        "id": canal_id,
        "nombre": nombre,
        "sub": sub,
        "avatarText": avatar,
        "ultima": ultimo["texto"] if ultimo else "",
        "hora": ultimo["hora"] if ultimo else "",
        "puedeEscribir": puede_escribir
    }


# ---------- qué ve cada rol ----------

def _config_profesor(usuario_id):

    with _cursor_db() as cursor:

        cursor.execute(
            """
            SELECT pmg.grupo_id, pmg.materia_id, g.nombre, m.nombre
            FROM profesor_materia_grupo pmg
            JOIN grupos g ON g.id = pmg.grupo_id
            JOIN materias m ON m.id = pmg.materia_id
            WHERE pmg.profesor_id = %s
            ORDER BY g.nombre, m.nombre
            """,
            (usuario_id,)
        )

        asignaciones = cursor.fetchall()

        salones = []

        for grupo_id, materia_id, grupo_nombre, materia_nombre in asignaciones:

            canal_id = _canal_grupo_materia(cursor, grupo_id, materia_id)

            salones.append((canal_id, grupo_nombre, materia_nombre))

        canal_admin = _canal_directo(cursor, usuario_id)

        canal_general = _canal_general(cursor)

        ids = [s[0] for s in salones] + [canal_admin, canal_general]

        ultimos = _ultimos_mensajes(cursor, ids)

    items_salones = [
        _item(canal_id, materia + " — " + grupo, "Grupo " + grupo,
              _avatar_materia(materia), ultimos, True)
        for canal_id, grupo, materia in salones
    ]

    return {

        "grupos": [
            {
                "titulo": "",
                "vacio": "Aún no tienes salones asignados. "
                         "Administración te asignará tus materias y grupos.",
                "items": items_salones
            },
            {
                "titulo": "Institucional",
                "vacio": "",
                "items": [
                    _item(canal_admin, "Administración",
                          "Chat con administración", "AD", ultimos, True),
                    _item(canal_general, "General",
                          "Avisos para toda la institución", "GE", ultimos, False),
                ]
            }
        ],

        "vacioGlobal": ""
    }


def _config_estudiante(usuario_id):
    """Devuelve (curso, config) del estudiante."""

    with _cursor_db() as cursor:

        cursor.execute(
            """
            SELECT eg.grupo_id, g.nombre, g.grado_id
            FROM estudiante_grupo eg
            JOIN grupos g ON g.id = eg.grupo_id
            WHERE eg.estudiante_id = %s
            """,
            (usuario_id,)
        )

        fila = cursor.fetchone()

        if not fila:

            return "Sin curso asignado", {
                "grupos": [],
                "vacioGlobal": "Todavía no estás matriculado en ningún grupo. "
                               "Pídele a administración que te asigne uno."
            }

        grupo_id, grupo_nombre, grado_id = fila

        # un canal por cada materia para el grupo del estudiante
        cursor.execute(
            """
            INSERT INTO canales (tipo, grupo_id, materia_id)
            SELECT 'grupo_materia', %s, m.id FROM materias m
            ON CONFLICT DO NOTHING
            """,
            (grupo_id,)
        )

        cursor.execute(
            """
            SELECT c.id, m.nombre
            FROM canales c
            JOIN materias m ON m.id = c.materia_id
            WHERE c.tipo = 'grupo_materia' AND c.grupo_id = %s
            ORDER BY m.id
            """,
            (grupo_id,)
        )

        materias = cursor.fetchall()

        canal_general = _canal_general(cursor)

        cursor.execute(
            """
            SELECT c.id, gr.nombre
            FROM canales c
            JOIN grados gr ON gr.id = c.grado_id
            WHERE c.tipo = 'coordinacion' AND c.grado_id = %s
              AND c.materia_id IS NULL
            """,
            (grado_id,)
        )

        canal_grado = cursor.fetchone()

        ids = [m[0] for m in materias] + [canal_general]

        if canal_grado:
            ids.append(canal_grado[0])

        ultimos = _ultimos_mensajes(cursor, ids)

    avisos = [
        _item(canal_general, "General", "Avisos para toda la institución",
              "GE", ultimos, False)
    ]

    if canal_grado:

        avisos.append(
            _item(canal_grado[0], _nombre_grado(canal_grado[1]),
                  "Avisos de coordinación para tu grado",
                  canal_grado[1] if len(canal_grado[1]) <= 2 else "GR",
                  ultimos, False)
        )

    items_materias = [
        _item(canal_id, nombre, "Canal de la materia",
              _avatar_materia(nombre), ultimos, False)
        for canal_id, nombre in materias
    ]

    return grupo_nombre, {

        "grupos": [
            {"titulo": "Avisos", "vacio": "", "items": avisos},
            {"titulo": "Materias", "vacio": "", "items": items_materias},
        ],

        "vacioGlobal": ""
    }


def _config_coordinador(usuario_id):

    with _cursor_db() as cursor:

        canal_general = _canal_general(cursor)

        grados = _canales_de_grados(cursor)

        canal_admin = _canal_directo(cursor, usuario_id)

        ids = [canal_general, canal_admin] + [g[0] for g in grados]

        ultimos = _ultimos_mensajes(cursor, ids)

    canales = [
        _item(canal_general, "General", "Todos los grados", "GE", ultimos, True)
    ]

    for canal_id, nombre in grados:

        canales.append(
            _item(canal_id, _nombre_grado(nombre),
                  "Anuncios para " + _nombre_grado(nombre).lower(),
                  nombre if len(nombre) <= 2 else "GR", ultimos, True)
        )

    return {

        "grupos": [
            {"titulo": "", "vacio": "", "items": canales},
            {
                "titulo": "Administración",
                "vacio": "",
                "items": [
                    _item(canal_admin, "Administración",
                          "Chat con administración", "AD", ultimos, True)
                ]
            }
        ],

        "vacioGlobal": ""
    }


def _config_administracion():

    with _cursor_db() as cursor:

        # un chat directo por cada coordinador y profesor
        cursor.execute(
            """
            INSERT INTO canales (tipo, usuario_id)
            SELECT 'directo', u.id FROM usuarios u
            WHERE u.rol IN ('coordinador', 'profesor')
            ON CONFLICT DO NOTHING
            """
        )

        cursor.execute(
            """
            SELECT u.nombre, u.usuario, u.rol, c.id
            FROM usuarios u
            JOIN canales c ON c.tipo = 'directo' AND c.usuario_id = u.id
            WHERE u.rol IN ('coordinador', 'profesor')
            ORDER BY u.nombre
            """
        )

        personas = cursor.fetchall()

        canal_general = _canal_general(cursor)

        grados = _canales_de_grados(cursor)

        ids = [p[3] for p in personas] + [canal_general] + [g[0] for g in grados]

        ultimos = _ultimos_mensajes(cursor, ids)

    def _persona(fila):

        nombre, usuario, rol, canal_id = fila

        return _item(canal_id, nombre, "@" + usuario,
                     _iniciales(nombre), ultimos, True)

    coordinadores = [_persona(p) for p in personas if p[2] == "coordinador"]

    profesores = [_persona(p) for p in personas if p[2] == "profesor"]

    items_grados = [
        _item(canal_general, "General", "Todos los grados", "GE", ultimos, True)
    ]

    for canal_id, nombre in grados:

        items_grados.append(
            _item(canal_id, _nombre_grado(nombre),
                  "Aviso para " + _nombre_grado(nombre).lower(),
                  nombre if len(nombre) <= 2 else "GR", ultimos, True)
        )

    return {

        "grupos": [
            {"titulo": "Coordinadores",
             "vacio": "Aún no hay coordinadores registrados.",
             "items": coordinadores},
            {"titulo": "Grados", "vacio": "", "items": items_grados},
            {"titulo": "Profesores",
             "vacio": "Aún no hay profesores registrados.",
             "items": profesores},
        ],

        "vacioGlobal": ""
    }


# ---------- permisos sobre un canal ----------

def _permisos_canal(cursor, usuario_id, rol, canal):
    """Devuelve (puede_ver, puede_escribir) para un usuario sobre un canal.
    `canal` = (tipo, grupo_id, materia_id, grado_id, usuario_id_del_canal)."""

    tipo, grupo_id, materia_id, grado_id, usuario_del_canal = canal

    if rol == "administracion":

        if tipo in ("coordinacion", "general", "directo"):
            return True, True

        return True, False          # puede leer los salones, no escribir en ellos

    if rol == "coordinador":

        if tipo in ("coordinacion", "general"):
            return True, True

        if tipo == "directo" and usuario_del_canal == usuario_id:
            return True, True

        return False, False

    if rol == "profesor":

        if tipo == "general":
            return True, False

        if tipo == "directo" and usuario_del_canal == usuario_id:
            return True, True

        if tipo == "grupo_materia":

            cursor.execute(
                """
                SELECT 1 FROM profesor_materia_grupo
                WHERE profesor_id = %s AND materia_id = %s AND grupo_id = %s
                """,
                (usuario_id, materia_id, grupo_id)
            )

            if cursor.fetchone():
                return True, True

        return False, False

    if rol == "estudiante":

        if tipo == "general":
            return True, False

        cursor.execute(
            """
            SELECT eg.grupo_id, g.grado_id
            FROM estudiante_grupo eg
            JOIN grupos g ON g.id = eg.grupo_id
            WHERE eg.estudiante_id = %s
            """,
            (usuario_id,)
        )

        fila = cursor.fetchone()

        if not fila:
            return False, False

        if tipo == "grupo_materia" and grupo_id == fila[0]:
            return True, False

        if tipo == "coordinacion" and grado_id == fila[1]:
            return True, False

        return False, False

    return False, False


# =========================================================
# RUTAS DE PÁGINAS (HTML)
# =========================================================

@app.route("/healthz")
def healthz():

    return "ok", 200


@app.route("/")
def raiz():

    if session.get("usuario_id"):

        destino = RUTA_POR_ROL.get(session.get("rol"))

        if destino:
            return redirect(destino)

    return render_template("pagina_bienvenida.html")


@app.route("/login", methods=["GET", "POST"])
def login():

    if request.method == "GET":

        return render_template("login.html")

    # ---- POST: formulario normal, sin JavaScript ----

    usuario = request.form.get("usuario")

    password = request.form.get("password")

    resultado = _iniciar_sesion(usuario, password)

    if not resultado.get("ok"):

        return render_template("login.html", error=resultado.get("error"))

    session["usuario_id"] = resultado["usuario_id"]

    session["nombre"] = resultado["nombre"]

    session["rol"] = resultado["rol"]

    session.permanent = True            # recordar la sesión (30 días)

    # ---- el condicional según el rol lo decide Python, no JS ----

    rol = resultado["rol"]

    if rol == "estudiante":

        return redirect(url_for("panel_estudiante"))

    elif rol == "profesor":

        return redirect(url_for("panel_profesor"))

    elif rol == "coordinador":

        return redirect(url_for("panel_coordinador"))

    elif rol == "administracion":

        return redirect(url_for("panel_administracion"))

    return redirect(url_for("raiz"))


@app.route("/profesor")
def panel_profesor():

    redireccion = _exige_rol("profesor")

    if redireccion:
        return redireccion

    return render_template(
        "profesores/dashboard.html",
        nombre=session.get("nombre"),
        config=_config_profesor(session["usuario_id"])
    )


@app.route("/estudiante")
def panel_estudiante():

    redireccion = _exige_rol("estudiante")

    if redireccion:
        return redireccion

    curso, config = _config_estudiante(session["usuario_id"])

    return render_template(
        "estudiantes/dashboard.html",
        curso=curso,
        config=config
    )


@app.route("/coordinador")
def panel_coordinador():

    redireccion = _exige_rol("coordinador")

    if redireccion:
        return redireccion

    return render_template(
        "coordinadores/dashboard.html",
        nombre=session.get("nombre"),
        config=_config_coordinador(session["usuario_id"])
    )


@app.route("/administracion")
def panel_administracion():

    redireccion = _exige_rol("administracion")

    if redireccion:
        return redireccion

    return render_template(
        "administracion/dashboard.html",
        nombre=session.get("nombre"),
        config=_config_administracion()
    )


# =========================================================
# ADMINISTRACIÓN: GESTIÓN (usuarios, grupos y asignaciones)
# Solo el rol "administracion" puede entrar aquí.
# =========================================================

def _volver_a_gestion(mensaje, categoria="ok"):

    flash(mensaje, categoria)

    return redirect(url_for("gestion_admin"))


@app.route("/administracion/gestion")
def gestion_admin():

    redireccion = _exige_rol("administracion")

    if redireccion:
        return redireccion

    with _cursor_db() as cursor:

        cursor.execute("SELECT id, nombre FROM grados ORDER BY id")
        grados = cursor.fetchall()

        cursor.execute(
            """
            SELECT g.id, g.nombre, gr.nombre
            FROM grupos g JOIN grados gr ON gr.id = g.grado_id
            ORDER BY gr.id, g.nombre
            """
        )
        grupos = cursor.fetchall()

        cursor.execute("SELECT id, nombre FROM materias ORDER BY id")
        materias = cursor.fetchall()

        cursor.execute(
            """
            SELECT u.id, u.nombre, u.usuario, u.rol, g.nombre
            FROM usuarios u
            LEFT JOIN estudiante_grupo eg ON eg.estudiante_id = u.id
            LEFT JOIN grupos g ON g.id = eg.grupo_id
            ORDER BY u.rol, u.nombre
            """
        )
        usuarios = cursor.fetchall()

        cursor.execute(
            """
            SELECT pmg.id, u.nombre, m.nombre, g.nombre
            FROM profesor_materia_grupo pmg
            JOIN usuarios u ON u.id = pmg.profesor_id
            JOIN materias m ON m.id = pmg.materia_id
            JOIN grupos g ON g.id = pmg.grupo_id
            ORDER BY u.nombre, g.nombre, m.nombre
            """
        )
        asignaciones = cursor.fetchall()

    return render_template(
        "administracion/gestion.html",
        nombre=session.get("nombre"),
        yo_id=session.get("usuario_id"),
        grados=grados,
        grupos=grupos,
        materias=materias,
        usuarios=usuarios,
        estudiantes=[u for u in usuarios if u[3] == "estudiante"],
        profesores=[u for u in usuarios if u[3] == "profesor"],
        asignaciones=asignaciones,
        roles=ROLES_VALIDOS
    )


@app.route("/administracion/gestion/usuario", methods=["POST"])
def gestion_crear_usuario():

    redireccion = _exige_rol("administracion")

    if redireccion:
        return redireccion

    rol = (request.form.get("rol") or "").strip().lower()

    if rol not in ROLES_VALIDOS:
        return _volver_a_gestion("Elige un rol válido.", "error")

    resultado = _registrar_usuario(
        request.form.get("nombre"),
        request.form.get("usuario"),
        request.form.get("password"),
        rol
    )

    if not resultado.get("ok"):
        return _volver_a_gestion(resultado.get("error", "No se pudo crear."), "error")

    grupo_id = request.form.get("grupo_id", type=int)

    if rol == "estudiante" and grupo_id:

        with _cursor_db() as cursor:

            cursor.execute(
                "INSERT INTO estudiante_grupo (estudiante_id, grupo_id) "
                "VALUES (%s, %s) ON CONFLICT (estudiante_id) DO NOTHING",
                (resultado["usuario_id"], grupo_id)
            )

    return _volver_a_gestion(
        "Usuario '%s' creado como %s." % (resultado["usuario"], rol)
    )


@app.route("/administracion/gestion/grupo", methods=["POST"])
def gestion_crear_grupo():

    redireccion = _exige_rol("administracion")

    if redireccion:
        return redireccion

    grado_id = request.form.get("grado_id", type=int)

    nombre = (request.form.get("nombre") or "").strip()

    if not grado_id or not nombre:
        return _volver_a_gestion("Elige un grado y escribe el nombre del grupo.", "error")

    if len(nombre) > 20:
        return _volver_a_gestion("El nombre del grupo es muy largo (máximo 20).", "error")

    with _cursor_db() as cursor:

        cursor.execute(
            "INSERT INTO grupos (grado_id, nombre) VALUES (%s, %s) "
            "ON CONFLICT DO NOTHING",
            (grado_id, nombre)
        )

        creado = cursor.rowcount

    if not creado:
        return _volver_a_gestion("Ese grupo ya existe en ese grado.", "error")

    return _volver_a_gestion("Grupo '%s' creado." % nombre)


@app.route("/administracion/gestion/matricula", methods=["POST"])
def gestion_matricular():

    redireccion = _exige_rol("administracion")

    if redireccion:
        return redireccion

    estudiante_id = request.form.get("estudiante_id", type=int)

    grupo_id = request.form.get("grupo_id", type=int)

    if not estudiante_id or not grupo_id:
        return _volver_a_gestion("Elige un estudiante y un grupo.", "error")

    with _cursor_db() as cursor:

        cursor.execute(
            "SELECT 1 FROM usuarios WHERE id = %s AND rol = 'estudiante'",
            (estudiante_id,)
        )

        if not cursor.fetchone():
            return _volver_a_gestion("Esa persona no es estudiante.", "error")

        # Regla: un estudiante solo puede estar en UN grupo.
        # Si ya tiene uno, no se mueve en silencio: hay que quitarlo primero.
        cursor.execute(
            """
            INSERT INTO estudiante_grupo (estudiante_id, grupo_id)
            VALUES (%s, %s)
            ON CONFLICT (estudiante_id) DO NOTHING
            """,
            (estudiante_id, grupo_id)
        )

        if not cursor.rowcount:

            cursor.execute(
                """
                SELECT g.nombre FROM estudiante_grupo eg
                JOIN grupos g ON g.id = eg.grupo_id
                WHERE eg.estudiante_id = %s
                """,
                (estudiante_id,)
            )

            actual = cursor.fetchone()

            return _volver_a_gestion(
                "Ese estudiante ya está matriculado en %s. Solo puede estar en "
                "un grupo: quítale la matrícula primero." % (actual[0] if actual else "otro grupo"),
                "error"
            )

    return _volver_a_gestion("Estudiante asignado al grupo.")


@app.route("/administracion/gestion/matricula/eliminar", methods=["POST"])
def gestion_quitar_matricula():

    redireccion = _exige_rol("administracion")

    if redireccion:
        return redireccion

    estudiante_id = request.form.get("estudiante_id", type=int)

    if not estudiante_id:
        return _volver_a_gestion("Elige un estudiante.", "error")

    with _cursor_db() as cursor:

        cursor.execute(
            "DELETE FROM estudiante_grupo WHERE estudiante_id = %s",
            (estudiante_id,)
        )

        quitada = cursor.rowcount

    if not quitada:
        return _volver_a_gestion("Ese estudiante no tenía matrícula.", "error")

    return _volver_a_gestion("Matrícula eliminada.")


@app.route("/administracion/gestion/usuario/eliminar", methods=["POST"])
def gestion_eliminar_usuario():

    redireccion = _exige_rol("administracion")

    if redireccion:
        return redireccion

    usuario_id = request.form.get("usuario_id", type=int)

    if not usuario_id:
        return _volver_a_gestion("Elige un usuario.", "error")

    if usuario_id == session.get("usuario_id"):
        return _volver_a_gestion("No puedes eliminar tu propia cuenta.", "error")

    with _cursor_db() as cursor:

        cursor.execute(
            "SELECT usuario, rol FROM usuarios WHERE id = %s",
            (usuario_id,)
        )

        fila = cursor.fetchone()

        if not fila:
            return _volver_a_gestion("Ese usuario no existe.", "error")

        # no dejar el sistema sin ninguna cuenta de administración
        if fila[1] == "administracion":

            cursor.execute(
                "SELECT COUNT(*) FROM usuarios WHERE rol = 'administracion'"
            )

            if cursor.fetchone()[0] <= 1:
                return _volver_a_gestion(
                    "No se puede eliminar la única cuenta de administración.",
                    "error"
                )

        # mensajes.emisor_id es RESTRICT: hay que borrar sus mensajes primero.
        # La matrícula, las asignaciones y su chat directo se borran solos (CASCADE).
        # archivos adjuntos que quedarán huérfanos (los suyos y los de su chat directo)
        cursor.execute(
            """
            SELECT archivo_clave FROM mensajes
            WHERE archivo_clave IS NOT NULL
              AND (emisor_id = %s
                   OR canal_id IN (SELECT id FROM canales
                                   WHERE tipo = 'directo' AND usuario_id = %s))
            """,
            (usuario_id, usuario_id)
        )

        claves = [f[0] for f in cursor.fetchall()]

        cursor.execute("DELETE FROM mensajes WHERE emisor_id = %s", (usuario_id,))

        cursor.execute("DELETE FROM usuarios WHERE id = %s", (usuario_id,))

    _borrar_archivos(claves)

    return _volver_a_gestion("Usuario '%s' eliminado." % fila[0])


@app.route("/administracion/gestion/asignacion", methods=["POST"])
def gestion_asignar_profesor():

    redireccion = _exige_rol("administracion")

    if redireccion:
        return redireccion

    profesor_id = request.form.get("profesor_id", type=int)

    materia_id = request.form.get("materia_id", type=int)

    grupo_id = request.form.get("grupo_id", type=int)

    if not profesor_id or not materia_id or not grupo_id:
        return _volver_a_gestion("Elige profesor, materia y grupo.", "error")

    with _cursor_db() as cursor:

        cursor.execute(
            "SELECT 1 FROM usuarios WHERE id = %s AND rol = 'profesor'",
            (profesor_id,)
        )

        if not cursor.fetchone():
            return _volver_a_gestion("Esa persona no es profesor.", "error")

        cursor.execute(
            """
            INSERT INTO profesor_materia_grupo (profesor_id, materia_id, grupo_id)
            VALUES (%s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (profesor_id, materia_id, grupo_id)
        )

        nuevo = cursor.rowcount

        _canal_grupo_materia(cursor, grupo_id, materia_id)

    if not nuevo:
        return _volver_a_gestion("Esa asignación ya existía.", "error")

    return _volver_a_gestion("Asignación creada.")


@app.route("/administracion/gestion/asignacion/eliminar", methods=["POST"])
def gestion_quitar_asignacion():

    redireccion = _exige_rol("administracion")

    if redireccion:
        return redireccion

    asignacion_id = request.form.get("asignacion_id", type=int)

    with _cursor_db() as cursor:

        cursor.execute(
            "DELETE FROM profesor_materia_grupo WHERE id = %s",
            (asignacion_id,)
        )

    return _volver_a_gestion("Asignación eliminada.")


@app.route("/administracion/gestion/materia", methods=["POST"])
def gestion_crear_materia():

    redireccion = _exige_rol("administracion")

    if redireccion:
        return redireccion

    nombre = (request.form.get("nombre") or "").strip()

    if not nombre or len(nombre) > 100:
        return _volver_a_gestion("Escribe un nombre de materia válido.", "error")

    with _cursor_db() as cursor:

        cursor.execute(
            "INSERT INTO materias (nombre) VALUES (%s) ON CONFLICT DO NOTHING",
            (nombre,)
        )

        creada = cursor.rowcount

    if not creada:
        return _volver_a_gestion("Esa materia ya existe.", "error")

    return _volver_a_gestion("Materia '%s' creada." % nombre)


@app.route("/administracion/gestion/password", methods=["POST"])
def gestion_cambiar_password():

    redireccion = _exige_rol("administracion")

    if redireccion:
        return redireccion

    nueva = request.form.get("password") or ""

    confirmar = request.form.get("confirmar") or ""

    if len(nueva) < 4:
        return _volver_a_gestion("La contraseña debe tener al menos 4 caracteres.", "error")

    if nueva != confirmar:
        return _volver_a_gestion("Las contraseñas no coinciden.", "error")

    with _cursor_db() as cursor:

        cursor.execute(
            "UPDATE usuarios SET contrasena_hash = %s WHERE id = %s",
            (_hash_password(nueva), session["usuario_id"])
        )

    return _volver_a_gestion("Contraseña actualizada.")


@app.route("/administracion/gestion/password/usuario", methods=["POST"])
def gestion_cambiar_password_usuario():

    redireccion = _exige_rol("administracion")

    if redireccion:
        return redireccion

    usuario_id = request.form.get("usuario_id", type=int)

    nueva = request.form.get("password") or ""

    confirmar = request.form.get("confirmar") or ""

    if not usuario_id:
        return _volver_a_gestion("Elige un usuario.", "error")

    if len(nueva) < 4:
        return _volver_a_gestion("La contraseña debe tener al menos 4 caracteres.", "error")

    if nueva != confirmar:
        return _volver_a_gestion("Las contraseñas no coinciden.", "error")

    with _cursor_db() as cursor:

        cursor.execute(
            "UPDATE usuarios SET contrasena_hash = %s WHERE id = %s RETURNING usuario",
            (_hash_password(nueva), usuario_id)
        )

        fila = cursor.fetchone()

    if not fila:
        return _volver_a_gestion("Ese usuario no existe.", "error")

    return _volver_a_gestion("Contraseña de '%s' actualizada." % fila[0])


@app.route("/logout")
def logout():

    # este navegador deja de recibir las notificaciones de este usuario
    endpoint = session.get("push_endpoint")

    if endpoint:

        try:

            with _cursor_db() as cursor:

                cursor.execute(
                    "DELETE FROM push_suscripciones WHERE endpoint = %s",
                    (endpoint,)
                )

        except Exception as e:

            print("No se pudo quitar la suscripción push:", e)

    session.clear()

    return redirect(url_for("login"))


# =========================================================
# RUTAS DE API (JSON)
# =========================================================

def _etiqueta_emisor(nombre, rol_emisor):

    etiqueta = nombre

    if rol_emisor == "administracion":
        etiqueta += " · Administración"
    elif rol_emisor == "coordinador":
        etiqueta += " · Coordinación"

    return etiqueta


def _mensaje_a_dict(mensaje_id, emisor_id, nombre, rol_emisor,
                    contenido, momento, usuario_actual_id,
                    archivo_nombre=None, archivo_tipo=None, archivo_tam=None,
                    reenviado_de=None):

    etiqueta = _etiqueta_emisor(nombre, rol_emisor)

    datos = {
        "id": mensaje_id,
        "de": etiqueta,
        "texto": contenido,
        "hora": _hora_burbuja(momento),
        "propio": emisor_id == usuario_actual_id,
        "archivo": None,
        "reenviadoDe": reenviado_de
    }

    if archivo_nombre:

        datos["archivo"] = {
            "nombre": archivo_nombre,
            "ext": _extension(archivo_nombre),
            "esImagen": archivo_tipo == "imagen",
            "tam": archivo_tam or 0,
            "url": "/api/archivos/%d" % mensaje_id
        }

    return datos


# =========================================================
# ARCHIVOS ADJUNTOS (fotos y documentos en los chats)
# ---------------------------------------------------------
# - Se guardan en la carpeta "uploads/" junto a app.py, con un
#   nombre aleatorio (nunca el que manda el usuario).
# - Solo se aceptan las extensiones de la lista y se comprueba
#   que el contenido coincida con el tipo (firma del archivo).
# - Para descargarlos hay que tener acceso al canal del mensaje
#   (se reutiliza _permisos_canal).
# =========================================================

import re
import uuid

from flask import send_from_directory

# En Render de pago: monta un disco en /var/data y define NEXO_UPLOAD_DIR=/var/data/uploads
UPLOAD_DIR = os.environ.get("NEXO_UPLOAD_DIR") or os.path.join(BASE_DIR, "uploads")

if EN_PRODUCCION and not os.environ.get("NEXO_UPLOAD_DIR"):
    print("[AVISO] NEXO_UPLOAD_DIR no está definido: los archivos adjuntos se "
          "perderán en cada reinicio. Usa un disco persistente (plan de pago).")

MAX_ADJUNTO_BYTES = 10 * 1024 * 1024          # 10 MB por archivo

app.config["MAX_CONTENT_LENGTH"] = MAX_ADJUNTO_BYTES + 512 * 1024

EXT_IMAGEN = {"png", "jpg", "jpeg", "gif", "webp"}

EXT_DOCUMENTO = {"pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx",
                 "txt", "csv", "odt", "ods", "odp", "rtf"}

MIME_IMAGEN = {
    "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "gif": "image/gif", "webp": "image/webp",
}

# primeros bytes que debe tener cada formato (los vacíos no se comprueban)
FIRMAS = {
    "png": [b"\x89PNG\r\n\x1a\n"],
    "jpg": [b"\xff\xd8\xff"], "jpeg": [b"\xff\xd8\xff"],
    "gif": [b"GIF87a", b"GIF89a"],
    "webp": [b"RIFF"],
    "pdf": [b"%PDF"],
    "docx": [b"PK\x03\x04"], "xlsx": [b"PK\x03\x04"], "pptx": [b"PK\x03\x04"],
    "odt": [b"PK\x03\x04"], "ods": [b"PK\x03\x04"], "odp": [b"PK\x03\x04"],
    "doc": [b"\xd0\xcf\x11\xe0"], "xls": [b"\xd0\xcf\x11\xe0"],
    "ppt": [b"\xd0\xcf\x11\xe0"],
    "rtf": [b"{\\rtf"],
}


def _extension(nombre):

    nombre = nombre or ""

    return nombre.rsplit(".", 1)[-1].lower() if "." in nombre else ""


def _limpiar_nombre(nombre):
    """Nombre solo para mostrar: sin rutas ni caracteres de control."""

    nombre = os.path.basename((nombre or "").replace("\\", "/"))

    nombre = re.sub(r"[\x00-\x1f\x7f]", "", nombre).strip()

    if len(nombre) > 120:

        ext = _extension(nombre)

        nombre = nombre[:110] + ("." + ext if ext else "")

    return nombre or "archivo"


def _guardar_adjunto(archivo):
    """Valida y guarda el archivo. Devuelve (datos, None) o (None, error)."""

    nombre = _limpiar_nombre(archivo.filename)

    ext = _extension(nombre)

    if ext not in EXT_IMAGEN and ext not in EXT_DOCUMENTO:
        return None, ("Tipo de archivo no permitido. Puedes enviar fotos "
                      "(png, jpg, gif, webp) y documentos (pdf, Word, Excel, "
                      "PowerPoint, txt, csv).")

    cabecera = archivo.stream.read(16)

    archivo.stream.seek(0)

    if not cabecera:
        return None, "El archivo está vacío."

    firmas = FIRMAS.get(ext)

    if firmas and not any(cabecera.startswith(f) for f in firmas):
        return None, "El contenido del archivo no coincide con su tipo (." + ext + ")."

    if ext == "webp" and cabecera[8:12] != b"WEBP":
        return None, "El contenido del archivo no coincide con su tipo (.webp)."

    os.makedirs(UPLOAD_DIR, exist_ok=True)

    clave = uuid.uuid4().hex + "." + ext

    ruta = os.path.join(UPLOAD_DIR, clave)

    archivo.save(ruta)

    tam = os.path.getsize(ruta)

    if tam > MAX_ADJUNTO_BYTES:
        os.remove(ruta)
        return None, "El archivo supera el máximo de 10 MB."

    return {
        "nombre": nombre,
        "clave": clave,
        "tipo": "imagen" if ext in EXT_IMAGEN else "documento",
        "tam": tam
    }, None


def _borrar_archivos(claves):

    for clave in claves or []:

        # las claves las genera el servidor, pero se comprueba igual
        if not clave or os.path.basename(clave) != clave:
            continue

        try:
            os.remove(os.path.join(UPLOAD_DIR, clave))
        except OSError:
            pass


@app.errorhandler(413)
def _archivo_demasiado_grande(_error):

    return jsonify({"ok": False, "error": "El archivo supera el máximo de 10 MB."}), 413


@app.route("/api/archivos/<int:mensaje_id>")
def api_archivo(mensaje_id):

    usuario_id = session.get("usuario_id")

    rol = session.get("rol")

    if not usuario_id:
        return jsonify({"ok": False, "error": "Tu sesión expiró. Vuelve a iniciar sesión."}), 401

    try:

        with _cursor_db() as cursor:

            cursor.execute(
                """
                SELECT m.archivo_clave, m.archivo_nombre, m.archivo_tipo,
                       c.tipo, c.grupo_id, c.materia_id, c.grado_id, c.usuario_id
                FROM mensajes m
                JOIN canales c ON c.id = m.canal_id
                WHERE m.id = %s AND m.archivo_clave IS NOT NULL
                """,
                (mensaje_id,)
            )

            fila = cursor.fetchone()

            if not fila:
                return jsonify({"ok": False, "error": "Ese archivo no existe."}), 404

            puede_ver, _ = _permisos_canal(cursor, usuario_id, rol, fila[3:8])

            if not puede_ver:
                return jsonify({"ok": False, "error": "No tienes acceso a este archivo."}), 403

    except psycopg2.Error as e:

        print("ERROR POSTGRESQL EN ARCHIVOS:", e)

        return jsonify({"ok": False, "error": "Error con la base de datos."}), 500

    clave, nombre, tipo = fila[0], fila[1], fila[2]

    es_imagen = tipo == "imagen"

    respuesta = send_from_directory(
        UPLOAD_DIR, clave,
        mimetype=MIME_IMAGEN.get(_extension(clave)) if es_imagen else None,
        as_attachment=not es_imagen,
        download_name=nombre
    )

    respuesta.headers["X-Content-Type-Options"] = "nosniff"

    respuesta.headers["Cache-Control"] = "private, max-age=3600"

    return respuesta


# =========================================================
# NOTIFICACIONES PUSH (llegan con la página o el navegador cerrados)
# ---------------------------------------------------------
# 1. El navegador se suscribe (chat.js) y guarda su "endpoint" aquí.
# 2. Al enviarse un mensaje, el servidor calcula QUIÉN puede ver ese
#    canal (mismas reglas que _canales_visibles) y le manda el aviso
#    cifrado al servicio de push del navegador (Google, Microsoft...).
# 3. El service worker (static/sw.js) lo muestra aunque la página
#    esté cerrada.
# Requisitos: el servidor encendido, internet, y abrir el programa con
# http://localhost:5000 (el navegador exige "localhost" o HTTPS).
# =========================================================

import json
import base64
from urllib.parse import urlparse

PUSH_DISPONIBLE = False

try:

    _asegurar_libreria("pywebpush")

    from pywebpush import webpush, WebPushException

    PUSH_DISPONIBLE = True

except Exception as e:

    print("")
    print("[AVISO] No se pudo preparar 'pywebpush':", e)
    print("        Todo funciona igual, pero las notificaciones con la página")
    print("        cerrada quedan desactivadas. Prueba:  py -m pip install pywebpush")
    print("")

def _ruta_vapid():
    """La clave privada de push viene de la variable VAPID_PRIVATE_KEY.

    En Render el disco es temporal: si la clave se guardara en un archivo, se
    regeneraría en cada reinicio y todas las suscripciones push quedarían
    inválidas. Por eso se lee del entorno y se escribe en un archivo temporal
    (pywebpush necesita una ruta).
    """

    pem = os.environ.get("VAPID_PRIVATE_KEY")

    if pem:

        import tempfile

        pem = pem.replace("\\n", "\n").strip()

        # Acepta la clave en varias líneas, en una línea con \n, o con espacios
        # (algunos campos de Render convierten los saltos de línea en espacios).
        import re, textwrap

        m = re.match(r"-----BEGIN ([A-Z ]+)-----(.*?)-----END \1-----", pem, re.S)

        if m:
            cuerpo = "".join(m.group(2).split())
            pem = "-----BEGIN %s-----\n%s\n-----END %s-----" % (
                m.group(1), "\n".join(textwrap.wrap(cuerpo, 64)), m.group(1))

        pem += "\n"

        ruta = os.path.join(tempfile.gettempdir(), "nexo_vapid.pem")

        with open(ruta, "w") as f:
            f.write(pem)

        try:
            os.chmod(ruta, 0o600)
        except OSError:
            pass

        return ruta

    if EN_PRODUCCION:
        print("[AVISO] Falta VAPID_PRIVATE_KEY: las notificaciones push se "
              "romperán en cada reinicio. Genera una con generar_vapid.py.")

    return os.path.join(BASE_DIR, "vapid_privada.pem")


RUTA_VAPID = _ruta_vapid()

# Algunos servicios push (Apple) exigen un correo real: define VAPID_CONTACT
PUSH_CONTACTO = os.environ.get("VAPID_CONTACT", "mailto:admin@nexo.local")

# solo se aceptan suscripciones de los servicios de push reales de los
# navegadores (evita que alguien haga que el servidor llame a otras URL)
HOSTS_PUSH = ("fcm.googleapis.com", ".push.services.mozilla.com",
              ".notify.windows.com", ".push.apple.com")


def _preparar_vapid():
    """Crea (la primera vez) o carga la clave VAPID del servidor.

    Devuelve la clave PÚBLICA en el formato que pide el navegador, o None.
    La privada queda en vapid_privada.pem junto a app.py (no la compartas).
    """

    try:

        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        if os.path.exists(RUTA_VAPID):

            with open(RUTA_VAPID, "rb") as f:
                clave = serialization.load_pem_private_key(f.read(), password=None)

        else:

            clave = ec.generate_private_key(ec.SECP256R1())

            pem = clave.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption()
            )

            with open(RUTA_VAPID, "wb") as f:
                f.write(pem)

            try:
                os.chmod(RUTA_VAPID, 0o600)
            except OSError:
                pass

        crudo = clave.public_key().public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint
        )

        return base64.urlsafe_b64encode(crudo).rstrip(b"=").decode()

    except Exception as e:

        print("[AVISO] No se pudieron preparar las claves de push:", e)

        return None


CLAVE_PUBLICA_PUSH = _preparar_vapid() if PUSH_DISPONIBLE else None

PUSH_ACTIVO = bool(PUSH_DISPONIBLE and CLAVE_PUBLICA_PUSH)


def _endpoint_valido(url):

    if not isinstance(url, str) or len(url) > 2000:
        return False

    partes = urlparse(url)

    if partes.scheme != "https" or not partes.hostname:
        return False

    host = partes.hostname.lower()

    for permitido in HOSTS_PUSH:

        if permitido.startswith("."):

            if host.endswith(permitido):
                return True

        elif host == permitido:

            return True

    return False


def _destinatarios_canal(cursor, canal_id, emisor_id):
    """(título del canal, ids de usuarios que pueden verlo), sin el emisor.

    Debe coincidir con _canales_visibles: lo que cada rol ve en pantalla.
    """

    cursor.execute(
        """
        SELECT c.tipo, c.grupo_id, c.materia_id, c.grado_id, c.usuario_id,
               ma.nombre, g.nombre, gr.nombre
        FROM canales c
        LEFT JOIN materias ma ON ma.id = c.materia_id
        LEFT JOIN grupos   g  ON g.id  = c.grupo_id
        LEFT JOIN grados   gr ON gr.id = c.grado_id
        WHERE c.id = %s
        """,
        (canal_id,)
    )

    fila = cursor.fetchone()

    if not fila:
        return None, []

    tipo, grupo_id, materia_id, grado_id, dueno, materia, grupo, grado = fila

    if tipo == "general":

        titulo = "General"

        cursor.execute("SELECT id FROM usuarios")

    elif tipo == "grupo_materia":

        titulo = "%s · %s" % (materia, grupo)

        cursor.execute(
            """
            SELECT estudiante_id FROM estudiante_grupo WHERE grupo_id = %s
            UNION
            SELECT profesor_id FROM profesor_materia_grupo
            WHERE materia_id = %s AND grupo_id = %s
            """,
            (grupo_id, materia_id, grupo_id)
        )

    elif tipo == "coordinacion":

        titulo = _nombre_grado(grado)

        cursor.execute(
            """
            SELECT eg.estudiante_id
            FROM estudiante_grupo eg
            JOIN grupos g ON g.id = eg.grupo_id
            WHERE g.grado_id = %s
            UNION
            SELECT id FROM usuarios WHERE rol IN ('coordinador', 'administracion')
            """,
            (grado_id,)
        )

    elif tipo == "directo":

        titulo = None          # se usa el nombre de quien escribe

        cursor.execute(
            "SELECT id FROM usuarios WHERE id = %s OR rol = 'administracion'",
            (dueno,)
        )

    else:

        return None, []

    return titulo, [f[0] for f in cursor.fetchall() if f[0] != emisor_id]


def _notificar_push(canal_id, emisor_id, de, texto):
    """Manda el aviso a todos los dispositivos suscritos. Corre en un hilo."""

    try:

        with _cursor_db() as cursor:

            titulo, ids = _destinatarios_canal(cursor, canal_id, emisor_id)

            if not ids:
                return

            cursor.execute(
                """
                SELECT id, endpoint, p256dh, auth
                FROM push_suscripciones
                WHERE usuario_id = ANY(%s)
                """,
                (ids,)
            )

            suscripciones = cursor.fetchall()

    except Exception as e:

        print("ERROR PREPARANDO PUSH:", e)

        return

    if not suscripciones:
        return

    datos = json.dumps({
        "titulo": titulo or de,
        "cuerpo": "%s: %s" % (de, texto[:140]),
        "canal": canal_id,
        "tag": "nexo-canal-%s" % canal_id
    })

    caducadas = []

    for sub_id, endpoint, p256dh, auth in suscripciones:

        try:

            webpush(
                subscription_info={"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth}},
                data=datos,
                vapid_private_key=RUTA_VAPID,
                vapid_claims={"sub": PUSH_CONTACTO},
                ttl=3600,
                timeout=10
            )

        except WebPushException as e:

            codigo = getattr(getattr(e, "response", None), "status_code", None)

            if codigo in (404, 410):
                caducadas.append(sub_id)          # el dispositivo ya no existe
            else:
                print("PUSH NO ENTREGADO (%s):" % codigo, e)

        except Exception as e:

            print("PUSH NO ENTREGADO:", e)

    if caducadas:

        try:

            with _cursor_db() as cursor:

                cursor.execute(
                    "DELETE FROM push_suscripciones WHERE id = ANY(%s)",
                    (caducadas,)
                )

        except Exception as e:

            print("No se pudieron limpiar suscripciones caducadas:", e)


def _lanzar_push(canal_id, emisor_id, de, texto):

    if not PUSH_ACTIVO:
        return

    threading.Thread(
        target=_notificar_push,
        args=(canal_id, emisor_id, de, texto),
        daemon=True
    ).start()


@app.route("/sw.js")
def service_worker():
    """Se sirve desde la raíz para que pueda mostrar notificaciones."""

    respuesta = send_from_directory(
        app.static_folder, "sw.js", mimetype="application/javascript"
    )

    respuesta.headers["Cache-Control"] = "no-cache"

    return respuesta


@app.route("/api/push/clave")
def api_push_clave():

    if not session.get("usuario_id"):
        return jsonify({"ok": False, "error": "Tu sesión expiró."}), 401

    return jsonify({"ok": PUSH_ACTIVO, "clave": CLAVE_PUBLICA_PUSH})


@app.route("/api/push/suscribir", methods=["POST"])
def api_push_suscribir():

    usuario_id = session.get("usuario_id")

    if not usuario_id:
        return jsonify({"ok": False, "error": "Tu sesión expiró."}), 401

    if not PUSH_ACTIVO:
        return jsonify({"ok": False, "error": "Las notificaciones push no están activas en el servidor."}), 503

    datos = request.get_json(silent=True) or {}

    endpoint = datos.get("endpoint")

    claves = datos.get("keys") or {}

    p256dh = claves.get("p256dh")

    auth = claves.get("auth")

    if (not _endpoint_valido(endpoint)
            or not isinstance(p256dh, str) or not isinstance(auth, str)
            or not (0 < len(p256dh) <= 200) or not (0 < len(auth) <= 100)):
        return jsonify({"ok": False, "error": "Suscripción no válida."}), 400

    try:

        with _cursor_db() as cursor:

            # si este navegador ya estaba suscrito (otro usuario), pasa a este
            cursor.execute(
                """
                INSERT INTO push_suscripciones (usuario_id, endpoint, p256dh, auth)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (endpoint) DO UPDATE
                SET usuario_id = EXCLUDED.usuario_id,
                    p256dh = EXCLUDED.p256dh,
                    auth = EXCLUDED.auth
                """,
                (usuario_id, endpoint, p256dh, auth)
            )

    except psycopg2.Error as e:

        print("ERROR POSTGRESQL EN PUSH:", e)

        return jsonify({"ok": False, "error": "Error con la base de datos."}), 500

    session["push_endpoint"] = endpoint

    return jsonify({"ok": True})


# =========================================================
# REENVIAR UN MENSAJE A OTRO CHAT DE NEXO
# ---------------------------------------------------------
# - Hay que poder VER el mensaje original y poder ESCRIBIR en el
#   chat de destino (se reutiliza _permisos_canal para ambos).
# - El mensaje nuevo queda a nombre de quien reenvía y guarda el
#   autor original en "reenviado_de".
# - Si trae archivo, se COPIA (así borrar uno no afecta al otro).
# =========================================================

import shutil


def _copiar_adjunto(clave):
    """Copia un archivo ya guardado y devuelve la clave de la copia (o None)."""

    if not clave or os.path.basename(clave) != clave:
        return None

    origen = os.path.join(UPLOAD_DIR, clave)

    if not os.path.isfile(origen):
        return None

    nueva = uuid.uuid4().hex + "." + _extension(clave)

    shutil.copyfile(origen, os.path.join(UPLOAD_DIR, nueva))

    return nueva


@app.route("/api/mensajes/<int:mensaje_id>/reenviar", methods=["POST"])
def api_reenviar_mensaje(mensaje_id):

    usuario_id = session.get("usuario_id")

    rol = session.get("rol")

    if not usuario_id:
        return jsonify({"ok": False, "error": "Tu sesión expiró. Vuelve a iniciar sesión."}), 401

    datos = request.get_json(silent=True) or {}

    destino_id = datos.get("canal_id")

    if not isinstance(destino_id, int) or isinstance(destino_id, bool):
        return jsonify({"ok": False, "error": "Elige a qué chat reenviar."}), 400

    clave_copia = None

    try:

        with _cursor_db() as cursor:

            cursor.execute(
                """
                SELECT m.contenido, m.archivo_nombre, m.archivo_clave,
                       m.archivo_tipo, m.archivo_tam, m.reenviado_de,
                       u.nombre, u.rol,
                       c.tipo, c.grupo_id, c.materia_id, c.grado_id, c.usuario_id
                FROM mensajes m
                JOIN usuarios u ON u.id = m.emisor_id
                JOIN canales  c ON c.id = m.canal_id
                WHERE m.id = %s
                """,
                (mensaje_id,)
            )

            origen = cursor.fetchone()

            if not origen:
                return jsonify({"ok": False, "error": "Ese mensaje no existe."}), 404

            puede_ver, _ = _permisos_canal(cursor, usuario_id, rol, origen[8:13])

            if not puede_ver:
                return jsonify({"ok": False, "error": "No tienes acceso a ese mensaje."}), 403

            cursor.execute(
                """
                SELECT tipo, grupo_id, materia_id, grado_id, usuario_id
                FROM canales WHERE id = %s
                """,
                (destino_id,)
            )

            destino = cursor.fetchone()

            if not destino:
                return jsonify({"ok": False, "error": "Ese chat no existe."}), 404

            _, puede_escribir = _permisos_canal(cursor, usuario_id, rol, destino)

            if not puede_escribir:
                return jsonify({"ok": False, "error": "No puedes escribir en ese chat."}), 403

            contenido, archivo_nombre, archivo_clave, archivo_tipo, archivo_tam, ya_reenviado = origen[:6]

            # si ya era un reenvío, se conserva el autor ORIGINAL
            autor = ya_reenviado or _etiqueta_emisor(origen[6], origen[7])

            if archivo_clave:

                clave_copia = _copiar_adjunto(archivo_clave)

                if not clave_copia:
                    return jsonify({"ok": False, "error": "El archivo original ya no está disponible."}), 410

            try:

                cursor.execute(
                    "INSERT INTO mensajes (canal_id, emisor_id, contenido, "
                    "archivo_nombre, archivo_clave, archivo_tipo, archivo_tam, reenviado_de) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id, timestamp",
                    (destino_id, usuario_id, contenido,
                     archivo_nombre if clave_copia else None,
                     clave_copia,
                     archivo_tipo if clave_copia else None,
                     archivo_tam if clave_copia else None,
                     autor)
                )

                nuevo_id, momento = cursor.fetchone()

            except Exception:

                if clave_copia:
                    _borrar_archivos([clave_copia])

                raise

            nuevo = _mensaje_a_dict(
                nuevo_id, usuario_id, session.get("nombre"), rol,
                contenido, momento, usuario_id,
                archivo_nombre if clave_copia else None,
                archivo_tipo if clave_copia else None,
                archivo_tam if clave_copia else None,
                autor
            )

            _lanzar_push(
                destino_id, usuario_id, nuevo["de"],
                contenido or ("Envió un archivo: " + (archivo_nombre or ""))
            )

            return jsonify({"ok": True, "canal_id": destino_id, "mensaje": nuevo})

    except psycopg2.Error as e:

        print("ERROR POSTGRESQL AL REENVIAR:", e)

        return jsonify({"ok": False, "error": "Error con la base de datos."}), 500


@app.route("/api/canales/<int:canal_id>/mensajes", methods=["GET", "POST"])
def api_mensajes_canal(canal_id):

    usuario_id = session.get("usuario_id")

    rol = session.get("rol")

    if not usuario_id:
        return jsonify({"ok": False, "error": "Tu sesión expiró. Vuelve a iniciar sesión."}), 401

    try:

        with _cursor_db() as cursor:

            cursor.execute(
                "SELECT tipo, grupo_id, materia_id, grado_id, usuario_id "
                "FROM canales WHERE id = %s",
                (canal_id,)
            )

            canal = cursor.fetchone()

            if not canal:
                return jsonify({"ok": False, "error": "Ese canal no existe."}), 404

            puede_ver, puede_escribir = _permisos_canal(cursor, usuario_id, rol, canal)

            if not puede_ver:
                return jsonify({"ok": False, "error": "No tienes acceso a este canal."}), 403

            # ---------- enviar un mensaje (texto y/o archivo) ----------
            if request.method == "POST":

                if not puede_escribir:
                    return jsonify({"ok": False, "error": "No puedes escribir en este canal."}), 403

                archivo = None

                if request.mimetype == "multipart/form-data":

                    contenido = (request.form.get("contenido") or "").strip()

                    archivo = request.files.get("archivo")

                    if archivo is not None and not archivo.filename:
                        archivo = None

                else:

                    datos = request.get_json(silent=True) or {}

                    contenido = (datos.get("contenido") or "").strip()

                if not contenido and archivo is None:
                    return jsonify({"ok": False, "error": "El mensaje está vacío."}), 400

                if len(contenido) > 2000:
                    return jsonify({"ok": False, "error": "El mensaje es muy largo (máximo 2000 caracteres)."}), 400

                adjunto = None

                if archivo is not None:

                    adjunto, error = _guardar_adjunto(archivo)

                    if error:
                        return jsonify({"ok": False, "error": error}), 400

                try:

                    cursor.execute(
                        "INSERT INTO mensajes (canal_id, emisor_id, contenido, "
                        "archivo_nombre, archivo_clave, archivo_tipo, archivo_tam) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id, timestamp",
                        (canal_id, usuario_id, contenido,
                         adjunto["nombre"] if adjunto else None,
                         adjunto["clave"] if adjunto else None,
                         adjunto["tipo"] if adjunto else None,
                         adjunto["tam"] if adjunto else None)
                    )

                    mensaje_id, momento = cursor.fetchone()

                except Exception:

                    if adjunto:
                        _borrar_archivos([adjunto["clave"]])

                    raise

                nuevo = _mensaje_a_dict(
                    mensaje_id, usuario_id, session.get("nombre"), rol,
                    contenido, momento, usuario_id,
                    adjunto["nombre"] if adjunto else None,
                    adjunto["tipo"] if adjunto else None,
                    adjunto["tam"] if adjunto else None
                )

                # aviso push a quienes pueden ver este canal (en segundo plano)
                _lanzar_push(
                    canal_id, usuario_id, nuevo["de"],
                    contenido or ("Envió un archivo: " + adjunto["nombre"])
                )

                return jsonify({"ok": True, "mensaje": nuevo})

            # ---------- leer mensajes ----------
            despues = request.args.get("despues", type=int)

            if despues is None:

                cursor.execute(
                    """
                    SELECT * FROM (
                        SELECT m.id, m.emisor_id, u.nombre, u.rol,
                               m.contenido, m.timestamp,
                               m.archivo_nombre, m.archivo_tipo, m.archivo_tam,
                               m.reenviado_de
                        FROM mensajes m
                        JOIN usuarios u ON u.id = m.emisor_id
                        WHERE m.canal_id = %s
                        ORDER BY m.id DESC
                        LIMIT 200
                    ) ultimos
                    ORDER BY id
                    """,
                    (canal_id,)
                )

            else:

                cursor.execute(
                    """
                    SELECT m.id, m.emisor_id, u.nombre, u.rol,
                           m.contenido, m.timestamp,
                           m.archivo_nombre, m.archivo_tipo, m.archivo_tam,
                           m.reenviado_de
                    FROM mensajes m
                    JOIN usuarios u ON u.id = m.emisor_id
                    WHERE m.canal_id = %s AND m.id > %s
                    ORDER BY m.id
                    """,
                    (canal_id, despues)
                )

            mensajes = [
                _mensaje_a_dict(f[0], f[1], f[2], f[3], f[4], f[5], usuario_id,
                                f[6], f[7], f[8], f[9])
                for f in cursor.fetchall()
            ]

            return jsonify({
                "ok": True,
                "mensajes": mensajes,
                "puedeEscribir": puede_escribir
            })

    except psycopg2.Error as e:

        print("ERROR POSTGRESQL EN MENSAJES:", e)

        return jsonify({"ok": False, "error": "Error con la base de datos."}), 500


# =========================================================
# NOTIFICACIONES
# ---------------------------------------------------------
# Un usuario solo recibe notificaciones de los canales que
# PUEDE VER (misma regla que _permisos_canal). Así, un mensaje
# de un profesor al canal "Matemáticas · 11-06" solo se le
# notifica a los estudiantes de 11-06 (y a quien tenga acceso
# a ese canal), nunca a otros grupos.
# =========================================================

def _canales_visibles(cursor, usuario_id, rol):
    """Lista de ids de canales que este usuario puede ver."""

    if rol == "estudiante":

        cursor.execute(
            """
            SELECT c.id
            FROM canales c
            WHERE c.tipo = 'general'
               OR (c.tipo = 'grupo_materia' AND c.grupo_id = (
                      SELECT eg.grupo_id FROM estudiante_grupo eg
                      WHERE eg.estudiante_id = %s))
               OR (c.tipo = 'coordinacion' AND c.grado_id = (
                      SELECT g.grado_id FROM estudiante_grupo eg
                      JOIN grupos g ON g.id = eg.grupo_id
                      WHERE eg.estudiante_id = %s))
            """,
            (usuario_id, usuario_id)
        )

    elif rol == "profesor":

        cursor.execute(
            """
            SELECT c.id
            FROM canales c
            WHERE c.tipo = 'general'
               OR (c.tipo = 'directo' AND c.usuario_id = %s)
               OR (c.tipo = 'grupo_materia' AND EXISTS (
                      SELECT 1 FROM profesor_materia_grupo p
                      WHERE p.profesor_id = %s
                        AND p.materia_id = c.materia_id
                        AND p.grupo_id = c.grupo_id))
            """,
            (usuario_id, usuario_id)
        )

    elif rol == "coordinador":

        cursor.execute(
            """
            SELECT c.id
            FROM canales c
            WHERE c.tipo IN ('coordinacion', 'general')
               OR (c.tipo = 'directo' AND c.usuario_id = %s)
            """,
            (usuario_id,)
        )

    elif rol == "administracion":

        # administración NO recibe aviso de cada mensaje de los salones
        cursor.execute(
            "SELECT id FROM canales "
            "WHERE tipo IN ('coordinacion', 'general', 'directo')"
        )

    else:

        return []

    return [f[0] for f in cursor.fetchall()]


@app.route("/api/notificaciones")
def api_notificaciones():

    usuario_id = session.get("usuario_id")

    rol = session.get("rol")

    if not usuario_id:
        return jsonify({"ok": False, "error": "Tu sesión expiró. Vuelve a iniciar sesión."}), 401

    despues = request.args.get("despues", type=int)

    try:

        with _cursor_db() as cursor:

            ids = _canales_visibles(cursor, usuario_id, rol)

            if not ids:
                return jsonify({"ok": True, "ultimo": despues or 0, "notificaciones": []})

            # primera consulta: solo fija el punto de partida, sin avisar
            # de mensajes antiguos
            if despues is None:

                cursor.execute(
                    "SELECT COALESCE(MAX(id), 0) FROM mensajes WHERE canal_id = ANY(%s)",
                    (ids,)
                )

                return jsonify({
                    "ok": True,
                    "ultimo": cursor.fetchone()[0],
                    "notificaciones": []
                })

            cursor.execute(
                """
                SELECT m.id, m.canal_id, m.emisor_id, m.contenido, m.timestamp,
                       u.nombre, u.rol,
                       c.tipo, ma.nombre, g.nombre, gr.nombre,
                       m.archivo_nombre
                FROM mensajes m
                JOIN canales  c  ON c.id  = m.canal_id
                JOIN usuarios u  ON u.id  = m.emisor_id
                LEFT JOIN materias ma ON ma.id = c.materia_id
                LEFT JOIN grupos   g  ON g.id  = c.grupo_id
                LEFT JOIN grados   gr ON gr.id = c.grado_id
                WHERE m.id > %s
                  AND m.emisor_id <> %s
                  AND m.canal_id = ANY(%s)
                ORDER BY m.id
                LIMIT 50
                """,
                (despues, usuario_id, ids)
            )

            notificaciones = []

            ultimo = despues

            for (mid, canal_id, emisor_id, contenido, momento,
                 nombre, rol_emisor, tipo, materia, grupo, grado,
                 archivo_nombre) in cursor.fetchall():

                texto = contenido[:140]

                if not texto and archivo_nombre:
                    texto = "Envió un archivo: " + archivo_nombre

                if tipo == "grupo_materia":
                    titulo = f"{materia} · {grupo}"
                elif tipo == "coordinacion":
                    titulo = _nombre_grado(grado)
                elif tipo == "general":
                    titulo = "General"
                else:
                    titulo = nombre

                notificaciones.append({
                    "id": mid,
                    "canal": canal_id,
                    "titulo": titulo,
                    "de": _mensaje_a_dict(
                        mid, emisor_id, nombre, rol_emisor,
                        contenido, momento, usuario_id
                    )["de"],
                    "texto": texto,
                    "hora": _hora_burbuja(momento)
                })

                ultimo = mid

            return jsonify({
                "ok": True,
                "ultimo": ultimo,
                "notificaciones": notificaciones
            })

    except psycopg2.Error as e:

        print("ERROR POSTGRESQL EN NOTIFICACIONES:", e)

        return jsonify({"ok": False, "error": "Error con la base de datos."}), 500


@app.route("/api/login", methods=["POST"])
def api_login():

    datos = request.get_json(silent=True) or {}

    resultado = _iniciar_sesion(
        datos.get("usuario"),
        datos.get("password")
    )

    if resultado.get("ok"):

        session["usuario_id"] = resultado["usuario_id"]

        session["nombre"] = resultado["nombre"]

        session["rol"] = resultado["rol"]

        session.permanent = True        # recordar la sesión (30 días)

        resultado["destino"] = RUTA_POR_ROL.get(resultado["rol"], "/")

    return jsonify(resultado)


# =========================================================
# PROBAR CONEXIÓN
# =========================================================

def _explicar_error_conexion(texto):
    """Traduce el mensaje de PostgreSQL a qué hacer, en palabras simples."""

    t = (texto or "").lower()

    pistas = []

    if "password" in t or "contraseña" in t or "autentific" in t or "authentication" in t:
        pistas.append(
            "La CONTRASEÑA es incorrecta (o el usuario no es el que crees).\n"
            "   Pon la real en DB_CONFIG dentro de app.py, o ejecuta:\n"
            "       set NEXO_DB_PASSWORD=tu_clave_real\n"
            "       py app.py"
        )

    if ("database" in t or "base de datos" in t) and ("does not exist" in t or "no existe" in t):
        pistas.append(
            "La base de datos '%s' NO EXISTE. Créala en pgAdmin: clic derecho en\n"
            "   Databases > Create > Database, con el nombre '%s'." % (DB_CONFIG["dbname"], DB_CONFIG["dbname"])
        )

    if ("role" in t or "el rol" in t) and ("does not exist" in t or "no existe" in t):
        pistas.append("El usuario '%s' NO EXISTE en PostgreSQL." % DB_CONFIG["user"])

    if ("refused" in t or "rechaz" in t or "could not connect" in t or "timeout" in t
            or "tiempo de espera" in t or "no se pudo conectar" in t):
        pistas.append(
            "PostgreSQL NO RESPONDE en %s:%s. Está apagado o usa otro puerto.\n"
            "   Inicia el servicio 'postgresql-x64-..' en Servicios de Windows y\n"
            "   revisa el puerto en pgAdmin (Propiedades del servidor > Conexión)."
            % (DB_CONFIG["host"], DB_CONFIG["port"])
        )

    if not pistas:
        pistas.append("Revisa usuario, contraseña, nombre de la base de datos y puerto.")

    return pistas


def _probar_conexion():

    conn = None

    try:

        print("")
        print("========================================")
        print("     CONECTANDO CON POSTGRESQL")
        print("========================================")
        print("")

        conn = _conectar_db()

        print("CONEXIÓN CORRECTA")
        print("")
        print("Base de datos:", DB_CONFIG["dbname"])
        print("Usuario:", DB_CONFIG["user"])
        print("Host:", DB_CONFIG["host"])
        print("Puerto:", DB_CONFIG["port"])
        print("")

        return True

    except Exception as e:

        # cubre OperationalError (ya con el mensaje real) y cualquier otro fallo
        print("")
        print("========================================")
        print("       ERROR DE CONEXIÓN")
        print("========================================")
        print("")
        print("Mensaje de PostgreSQL:")
        print("  ", str(e).strip() or type(e).__name__)
        print("")
        print("Qué hacer:")

        for numero, pista in enumerate(_explicar_error_conexion(str(e)), 1):
            print("%d. %s" % (numero, pista))

        print("")
        print("Datos usados: host=%s puerto=%s base=%s usuario=%s" % (
            DB_CONFIG["host"], DB_CONFIG["port"], DB_CONFIG["dbname"], DB_CONFIG["user"]))
        print("")

        return False

    finally:

        if conn:
            conn.close()


# =========================================================
# ABRIR EL NAVEGADOR AUTOMÁTICAMENTE
# =========================================================

def _abrir_navegador():

    webbrowser.open("http://127.0.0.1:5000/")


# =========================================================
# INICIAR APLICACIÓN
# =========================================================


def _arranque_produccion():
    """gunicorn importa app.py (no ejecuta __main__): aquí se prepara la base."""

    import time

    for intento in range(1, 6):

        try:

            _inicializar_db()

            return

        except Exception as e:

            print("Intento %d/5 de preparar la base de datos falló: %s" % (intento, e))

            time.sleep(3)

    raise RuntimeError("No se pudo preparar la base de datos de Nexo.")


if __name__ != "__main__":

    _arranque_produccion()


if __name__ == "__main__":

    if not _probar_conexion():
        input("\nPresiona ENTER para cerrar...")
        sys.exit(1)

    try:
        _inicializar_db()

    except Exception:
        print("")
        print(traceback.format_exc())
        input(
            "\nNo se pudo inicializar PostgreSQL. "
            "Presiona ENTER para cerrar..."
        )
        sys.exit(1)

    print("")
    print("========================================")
    print("             INICIANDO NEXO")
    print("========================================")
    print("")
    print("Abriendo en: http://127.0.0.1:5000/")
    print("")

    for _archivo in ("static/chat.js",):
        if not os.path.exists(os.path.join(BASE_DIR, _archivo)):
            print(
                "AVISO: falta el archivo",
                _archivo,
                "- el chat no funcionará sin él."
            )

    threading.Timer(1, _abrir_navegador).start()

    # Solo para uso local. En Render se usa gunicorn (ver render.yaml).
    app.run(
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "5000")),
        debug=os.environ.get("NEXO_DEBUG") == "1",
        use_reloader=False
    )
