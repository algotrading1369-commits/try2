# ORBIT en Contabo: Ubuntu 24.04 LTS

Preparación para **una cuenta y una instalación** en un VPS Ubuntu 24.04 LTS
con systemd. El instalador todavía debe validarse en el VPS real; las pruebas
automáticas no demuestran disponibilidad 24/7 ni reconexión real de Gameforge.

## Arquitectura

- Código y entorno Python: `/opt/ogame`, propiedad de root.
- Configuración, perfil nuevo y órdenes: `/var/lib/ogame`, usuario `ogame`.
- `ogame.service`: panel privado y supervisor. El panel controla su proceso
  hijo del bot; el supervisor recupera caídas con espera de 5–300 segundos.
- La intención Iniciar/Detener se conserva en `runtime/vps_intent.json`.
  Una parada manual no se deshace tras reiniciar el servicio o el VPS.
- Pantalla virtual Xvfb con autorización X11; no escucha por TCP.
- Panel: `127.0.0.1:18765`, accesible mediante túnel SSH.
- Clave de Lobby cifrada para el host por `systemd-creds`; systemd la entrega
  al servicio mediante su directorio privado de credenciales.

El usuario del bot no necesita sudo ni acceso SSH. Las operaciones de instalación
y alta de credenciales se hacen desde la cuenta administrativa del servidor.

## 1. Instalar sin iniciar el bot

Contrata Ubuntu 24.04 LTS. Entra por SSH con la cuenta administrativa que te
proporcione el proveedor. Sustituye `IP_DEL_VPS` en los ejemplos por tu IP real.

```bash
sudo apt-get update
sudo apt-get install -y git
git clone https://github.com/algotrading1369-commits/try2.git ogame-src
cd ogame-src
sudo bash deploy/linux/install.sh
```

El script instala dependencias, Chromium, Xvfb, un visor VNC y las unidades.
No inicia el bot ni abre puertos en el firewall. No modifica tu configuración
SSH. Conserva el acceso administrativo que ya funciona.

## 2. Migrar configuración y diario de órdenes

**Detén el bot del PC antes de copiar los archivos y mantenlo detenido durante
la prueba del VPS.** Espera a que el navegador del bot se cierre. Así se cierran
las conexiones SQLite y no quedan dos programas enviando órdenes a la cuenta.

Desde PowerShell, situado en la carpeta `Main Bot` del PC:

```powershell
scp ".\config.yaml" root@IP_DEL_VPS:/var/lib/ogame/windows-config.yaml
scp ".\runtime\orders.sqlite3" root@IP_DEL_VPS:/var/lib/ogame/runtime/orders.sqlite3
```

Usa tu usuario administrativo en lugar de `root` si has configurado otro acceso;
en ese caso sube primero a su carpeta personal y mueve los archivos con sudo.
El diario conserva confirmaciones y órdenes inciertas: **no lo borres ni lo
sustituyas por una base vacía para evitar un bloqueo**. No copies el perfil de
Chrome ni cookies del PC. Las credenciales Windows se configuran de nuevo aquí.

En el servidor:

```bash
cd /opt/ogame
sudo .venv/bin/python -m ogame_bot.vps_config /var/lib/ogame/windows-config.yaml /var/lib/ogame/config.yaml
sudo chown ogame:ogame /var/lib/ogame/config.yaml /var/lib/ogame/runtime/orders.sqlite3
sudo chmod 600 /var/lib/ogame/config.yaml /var/lib/ogame/runtime/orders.sqlite3
```

El migrador conserva cuenta, objetivos e IDs, adapta todas las rutas y el
navegador a Linux, y deja **todas las ejecuciones reales desactivadas**. No
sobrescribe un destino existente. Si todavía no hay diario porque nunca se ha
enviado ninguna orden, omite su copia y sus comandos chown/chmod.

Telegram queda desactivado en la primera migración: sus variables de entorno
del PC no se transfieren. Reconfigúralo antes de activar las notificaciones.
El instalador no configura un proveedor externo de alertas ni envía mensajes.

## 3. Guardar el acceso al Lobby

```bash
sudo bash /opt/ogame/deploy/linux/configure_access.sh
```

Introduce correo y contraseña en la consola SSH. La contraseña no se muestra ni
se pasa como argumento. El asistente entrega el contenido por stdin a
`systemd-creds encrypt --with-key=host` y solo escribe el resultado cifrado en
`/etc/credstore.encrypted/ogame-lobby`, con permisos 600.

El secreto está vinculado al jugador y universo configurados; el bot solo lo
lee desde `$CREDENTIALS_DIRECTORY/ogame-lobby`, dentro del servicio. La variable
contiene una ruta, no la contraseña. No se usa texto plano como alternativa,
Secret Service, un escritorio desbloqueado ni contraseñas del navegador.
Root administra la clave de cifrado del host. Al cambiar de servidor debes
configurar de nuevo el acceso; este secreto no se exporta en las copias.

Para desactivar y retirar el acceso:

```bash
cd /opt/ogame
sudo .venv/bin/python -m ogame_bot.credentials remove --config /var/lib/ogame/config.yaml
sudo systemctl restart ogame.service
```

## 4. Arrancar y entrar al panel privado

```bash
sudo systemctl enable --now ogame.service
sudo systemctl enable --now ogame-backup.timer
```

La primera vez el panel espera tu orden; aún no hay intención de arranque.
Desde tu PC abre el túnel y deja esa consola abierta:

```powershell
ssh -N -L 18765:127.0.0.1:18765 root@IP_DEL_VPS
```

Abre **http://127.0.0.1:18765** en el navegador del PC. No uses la IP pública
del VPS ni abras 18765 en el firewall. El número local y remoto deben coincidir
para conservar las comprobaciones Host/Origin del panel. Se usa 18765 para no
interferir con el panel local de Windows en 8765.

Inicia primero en modo **Solo sesión**. Comprueba que accede al universo correcto.
Detén ese modo y elige el modo completo cuando hayas revisado la cuenta y las
órdenes pendientes. Activa desde el panel únicamente las funciones reales que
quieras probar. Tras reiniciar el VPS se recupera la última intención y modo;
si pulsaste Detener, el bot permanece detenido y el panel sigue disponible.

## 5. Ver el navegador y resolver verificaciones

El bot usa un navegador con pantalla virtual, por lo que puedes intervenir si
Gameforge pide CAPTCHA, código o acceso manual. No se automatizan esos desafíos.
En una consola SSH del servidor ejecuta:

```bash
sudo bash /opt/ogame/deploy/linux/view_browser.sh
```

La primera vez elige una contraseña para el visor VNC, distinta de la del juego.
En otra consola de tu PC abre:

```powershell
ssh -N -L 5900:127.0.0.1:5900 root@IP_DEL_VPS
```

Conecta tu visor VNC a `127.0.0.1:5900` con esa contraseña. Verás la pantalla del
navegador del bot y podrás completar el acceso personalmente. El visor escucha
solo en localhost y se cierra con Ctrl+C en su consola SSH. No abras 5900 ni
puertos X11 en el firewall. El panel y VNC no se publican por HTTP en Internet.

## 6. Comprobaciones y mantenimiento

```bash
sudo systemctl status ogame.service ogame-display.service
sudo journalctl -u ogame.service -n 80 --no-pager
sudo tail -n 80 /var/lib/ogame/runtime/logs/session.log
sudo systemctl list-timers ogame-backup.timer
```

Tras una caída del hijo, el supervisor mantiene el panel y vuelve a iniciar el
bot con espera creciente. Una caída del supervisor se recupera con systemd.
Las órdenes preparadas o inciertas conservan las protecciones del diario.
Esto no garantiza que el juego acepte el acceso ni que un CAPTCHA se resuelva.

Prueba una parada e inicio desde el panel, una desconexión del juego, un reinicio
del servicio y finalmente un reinicio del VPS. Revisa órdenes y registros antes
de dejarlo funcionar 24–48 horas, incluyendo el corte nocturno de Gameforge.

Para actualizar código: detén primero el bot desde el panel, actualiza el clon,
ejecuta otra vez el instalador y reinicia `ogame.service`. No borres configuración,
perfil ni diario. Evita actualizar durante una orden de flota en curso.

## 7. Copias de seguridad

El temporizador hace una copia diaria en `/var/backups/ogame`, con siete archivos
y permisos privados. Incluye configuración, JSON de estado e intención y una
copia coherente de SQLite mediante su API de backup. Excluye perfiles, cookies,
logs, credenciales y la clave del host. Puedes ejecutarla manualmente:

```bash
sudo systemctl start ogame-backup.service
```

Estas copias están en el mismo VPS: descarga periódicamente una copia a tu PC
para cubrir la pérdida del servidor. Antes de restaurar, detén el servicio y
verifica cuenta, universo y órdenes inciertas; no se restaura automáticamente.

Referencias: [credenciales systemd](https://www.freedesktop.org/software/systemd/man/systemd.exec.html#Credentials),
[systemd-creds](https://www.freedesktop.org/software/systemd/man/systemd-creds.html),
[instalación Playwright](https://playwright.dev/python/docs/browsers).
