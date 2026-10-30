"""Genera una clave privada VAPID NUEVA para las notificaciones push.

Uso:
    python generar_vapid.py

Imprime la clave en UNA SOLA LINEA. Copia esa línea completa y pégala en
Render > tu servicio > Environment > VAPID_PRIVATE_KEY.
No la guardes en el repositorio.
"""

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

clave = ec.generate_private_key(ec.SECP256R1())

pem = clave.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.TraditionalOpenSSL,
    serialization.NoEncryption(),
).decode().strip()

print()
print("COPIA LA SIGUIENTE LINEA COMPLETA:")
print()
print(pem.replace("\n", "\\n"))
print()
