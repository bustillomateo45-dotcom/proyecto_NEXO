# Publicar Nexo en Render

## 1. Antes de subir a GitHub
- Esta carpeta ya NO incluye `vapid_privada.pem` ni `nexo.sql`. El `.gitignore` los excluye.
- La clave privada anterior quedó expuesta en el chat: **no la reutilices**. Genera una nueva:
  `python generar_vapid.py` (necesita `pip install cryptography`).

## 2. Crear el proyecto en Render
1. Sube esta carpeta a un repositorio de GitHub (con `app.py`, `templates/`, `static/`, `requirements.txt`, `render.yaml` en la raíz).
2. En Render: **New > Blueprint** y elige el repositorio. Crea la base `nexo-db` y el servicio `nexo`.
3. Cuando lo pida, escribe:
   - `NEXO_ADMIN_PASS`: contraseña de administrador (la primera vez que arranque).
   - `VAPID_PRIVATE_KEY`: la línea que imprimió `generar_vapid.py`.
4. Cambia `VAPID_CONTACT` por un correo real.

El esquema de la base se crea solo al arrancar. Entra con el usuario `admin` y la contraseña que pusiste.

## 3. Variables de entorno
| Variable | Para qué |
|---|---|
| `DATABASE_URL` | La entrega Render desde la base (automática) |
| `SECRET_KEY` | Firma de sesiones (se genera sola) |
| `NEXO_ADMIN_USER` / `NEXO_ADMIN_PASS` | Administrador inicial (solo si no existe ninguno) |
| `VAPID_PRIVATE_KEY` / `VAPID_CONTACT` | Notificaciones push |
| `NEXO_UPLOAD_DIR` | Carpeta de adjuntos (solo con disco persistente) |

Si no defines `NEXO_ADMIN_PASS` y la base está vacía, se genera una contraseña aleatoria y aparece en los **Logs** del servicio.

## 4. Limitaciones del plan gratis
- El servicio se duerme a los 15 min sin tráfico (el primer acceso tarda ~1 min).
- La base gratis caduca (revisa el plazo en Render).
- Los adjuntos (fotos/documentos) se pierden en cada reinicio. Para conservarlos: plan Starter + disco (ver comentarios en `render.yaml`).

## 5. Pasar tus datos actuales (opcional)
`nexo.sql` tiene tus usuarios y mensajes. Para importarlo a la base de Render, usa la **External Database URL** de Render:
`psql "<EXTERNAL_DATABASE_URL>" -f nexo.sql`
Hazlo ANTES del primer arranque o reinicia el servicio después. No subas `nexo.sql` a GitHub.

## 6. Probar en tu PC
Sigue funcionando como antes: `python app.py` (usa las variables `NEXO_DB_*`).
