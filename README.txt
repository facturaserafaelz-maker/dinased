DINASED — CONTROL LOCAL (PROTOTIPO INICIAL)
===========================================

IMPORTANTE
----------
Este paquete es un prototipo de demostración, no una versión certificada para uso institucional.
Antes de usar datos reales, debe revisarlo una persona con experiencia en seguridad informática.
La cédula, el celular y los movimientos son datos personales. Pruebe primero con datos ficticios.
La aplicación NO verifica oficialmente la validez de la cédula ni prueba por sí sola la identidad física.
El GPS del navegador puede ser manipulado o impreciso.
El mecanismo de clave criptográfica es una demostración mediante Web Crypto; no equivale a WebAuthn/passkeys
con verificación biométrica. La aprobación administrativa es obligatoria antes de marcar.

QUÉ NECESITA
------------
1. Una computadora Windows que permanecerá encendida.
2. Conexión a la red Wi-Fi local.
3. Python 3.12 o 3.13 desde https://www.python.org/downloads/windows/
   Durante la instalación marque "Add python.exe to PATH".
4. Un navegador moderno.

PRIMERA PRUEBA EN LA COMPUTADORA
--------------------------------
1. Descomprima la carpeta DINASED_Control_Local en Documentos o Escritorio.
2. Instale Python desde el sitio oficial si aún no está instalado.
3. Entre en la carpeta y haga doble clic en iniciar_windows.bat.
4. Espere mientras instala las bibliotecas. La primera vez puede tardar.
5. Abra el navegador y vaya a:
   http://127.0.0.1:8080
6. Acceso inicial del administrador:
   Usuario: admin
   Contraseña: Cambiar123!
7. Antes de utilizarlo, cambie la contraseña predeterminada. Este prototipo no trae una pantalla
   para cambiarla: la forma recomendada para una instalación seria es implementar esa función y
   eliminar la cuenta predeterminada durante el despliegue. Para una prueba aislada, no exponga
   el sistema a otras redes con estas credenciales.

ACCESO DESDE OTROS EQUIPOS EN LA RED
------------------------------------
1. En Windows abra Inicio, escriba cmd y pulse Enter.
2. Escriba: ipconfig
3. Busque "Dirección IPv4" del adaptador Wi-Fi (por ejemplo 192.168.1.50).
4. En otro equipo conectado al mismo Wi-Fi, abra:
   http://192.168.1.50:8080
   Sustituya la IP por la que muestra su computadora.
5. Si no abre, revise el Firewall de Windows y que ambos equipos estén en la misma red.
   No abra ni reenvíe el puerto 8080 desde el router hacia Internet.

IMPORTANTE SOBRE GPS Y HTTPS
----------------------------
Los navegadores modernos normalmente solo permiten geolocalización y Web Crypto en un contexto seguro.
http://127.0.0.1 es una excepción en la misma computadora, pero la IP LAN (como http://192.168.1.50)
no se considera segura en muchos navegadores. Por eso, para usar el registro desde celulares se debe
configurar HTTPS para el nombre local del servidor y confiar en el certificado en los equipos cliente.
Esto requiere un nombre local y un certificado válido/confiable. No se debe ignorar una advertencia
del navegador ni instalar certificados desconocidos. Solicite apoyo técnico para preparar HTTPS local.
No es suficiente activar un ajuste de GPS en el teléfono.

CONFIGURAR LA GEOCERCA
----------------------
1. Inicie sesión como administrador.
2. Entre a "Geocerca".
3. Ingrese el nombre del lugar, latitud, longitud, radio y precisión GPS máxima.
4. Para obtener coordenadas, abra un mapa confiable, mantenga pulsado o haga clic en el punto y copie
   las coordenadas. Compruebe que el formato decimal sea correcto.
5. Guarde y pruebe con una ubicación ficticia primero.
Valores iniciales son ejemplos, NO son coordenadas oficiales de su instalación.

CÓDIGO QR
---------
1. Entre al panel desde la computadora.
2. Pulse "Descargar QR fijo".
3. Imprima el QR solo después de que la URL final del sistema esté definida.
4. Si cambia la IP o el nombre local del servidor, el QR descargado anteriormente podría dejar de servir.
5. El QR no es una prueba de identidad: cualquiera puede fotografiarlo. Por eso existen la aprobación
   del usuario y la comprobación criptográfica del dispositivo.

CÓMO PROBAR EL FLUJO
--------------------
1. Abra /register desde un navegador compatible en un contexto HTTPS seguro.
2. Complete los cuatro campos y pulse "Vincular este celular".
3. Envíe la solicitud.
4. El administrador entra en "Usuarios" y aprueba la cuenta.
5. En el mismo celular, escanee el QR o abra /scan, ingrese la cédula y siga a la marcación.
6. Ingrese la cédula, permita GPS y pulse "Validar y registrar".
7. El primer movimiento válido será ENTRADA; el siguiente será SALIDA, y así sucesivamente.

LIMITACIONES CONOCIDAS DEL PROTOTIPO
------------------------------------
- El QR apunta a una pantalla de entrada común: solicita la cédula y deriva a inscripción si no existe
  un usuario, o a marcación si el usuario ya está aprobado y vinculado.
- La clave privada se conserva en IndexedDB del navegador. Borrar datos del navegador o cambiar de
  celular puede perderla; el administrador deberá revocar el dispositivo y volver a vincularlo.
- La vinculación criptográfica no comprueba por sí misma que la persona inscrita sea la titular de la
  cédula. Requiere comprobación institucional antes de aprobar.
- La localización web no es una garantía contra GPS falsificado.
- La base incluida es SQLite (SQL) para facilitar la primera instalación. Para muchos usuarios o uso
  institucional, migre a PostgreSQL con apoyo técnico.
- No se incluyen exportaciones, cambio de contraseña del administrador, recuperación de cuenta,
  controles avanzados de acceso, HTTPS automático ni pruebas de seguridad.
- Los cambios y eliminaciones de usuarios necesitan una política de retención. No borre registros reales
  sin autorización y sin conservar la auditoría apropiada.
- No almacene datos reales hasta que la entidad responsable apruebe el tratamiento de datos personales.

COPIAS DE SEGURIDAD
------------------
El archivo dinased.db contiene la base de datos. Detenga la aplicación y copie el archivo a un lugar
protegido y autorizado. No envíe la base por correo ni la guarde en una nube personal sin autorización.

PARA DETENER
------------
Cierre la ventana negra donde está corriendo el servidor o pulse Ctrl+C.


NOTA DE SEGURIDAD: El administrador inicial no dispone todavía de una pantalla de cambio de contraseña.
No use este prototipo en una red institucional compartida sin que un técnico agregue cambio obligatorio de
contraseña, protección CSRF, HTTPS confiable, roles detallados, límites de intentos y revisión de seguridad.
