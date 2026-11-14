# Panel local

Abre `Abrir_panel.bat`. La interfaz está en `http://127.0.0.1:8765` y el servicio
debe seguir abierto mientras lo utilices. No se publica en Internet ni expone
el perfil del navegador, cookies o contraseñas. No hay recursos de terceros.

La vista general muestra el estado del proceso, lecturas con su antigüedad,
progreso de la sonda, órdenes recientes, módulos y diagnóstico. "Proceso
activo" no significa por sí solo que la sesión del juego esté autenticada.
Si se pide acceso manual, entra en el navegador del bot.

Puedes iniciar el bot desde el panel y solicitar su parada cooperativa. Solo
se detiene un proceso iniciado por ese mismo servicio; un bot abierto con
BAT se detiene con Ctrl+C en su consola. No se mata otro proceso por su PID.
Si cierras solo el servicio del panel, el bot conserva su propio proceso;
detén primero el bot si quieres cerrar ambos.

La configuración se edita con el bot detenido. Se valida antes de guardar,
con control de versión y copia de respaldo en `runtime/config-panel-*.yaml`.
Se pueden ajustar prioridades, reservas, intervalos, preparación de sonda y
ventana/misión de defensa. Los cambios se aplican al próximo arranque. El
panel mantiene los envíos de flota en simulación mientras su validación real
siga pendiente. El modo real limitado de sonda se identifica por separado.

El historial de fabricación no se borra desde el panel. La preparación de
una sonda termina tras su confirmación y no se repite al reiniciar.

El panel acepta conexiones solo en loopback y comprueba Host, Origin y un
token de sesión para las operaciones que modifican datos. No habilites
reenvíos de puerto ni cambies la dirección de escucha para exponerlo.
