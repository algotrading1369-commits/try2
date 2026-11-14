# Panel local de pruebas

Abre `Abrir_panel.bat` y deja su consola abierta. La interfaz está en
`http://127.0.0.1:8765`. Recarga la página tras actualizar el código.
Los cambios de Python requieren reiniciar también el servicio del panel.

## Pestañas

- Construcción: cola de objetivos reordenable o selección inteligente por ROI
  estimado, reservas, energía solar y límites. En ROI solo se admiten minas y
  se requieren velocidad económica y temperaturas para deuterio. En modo de
  prioridades, un objetivo de nivel se completa antes del siguiente.
- Investigación: cola de objetivos reordenable, una investigación a la vez.
- Expediciones: orígenes, composición, permanencia, clase, top 1 y paralelismo.
- Hangar: preparación limitada de una sonda con sus requisitos. Su historial
  impide repetirla tras reiniciar. Todavía no hay una cola general de naves.
- Defensa: vigilancia, ventanas de reacción, frecuencia y alertas Telegram.
- FleetSave: evacuación reactiva ante ataque y transportes a cuerpos propios.
  No implementa salida programada por hora ni cancelación automática. Un
  transporte de recursos no garantiza que la flota quede a salvo.
- Modo Scanner: intervalos, vigencia, cuerpos por lectura y reciclaje cercano.
- General: límites de flotas, capacidades verificadas, sesión y navegador.
- Registros: mensajes y filtros; cada módulo muestra también su último informe.

Los controles guardan la configuración real, validan el documento completo y
conservan una copia anterior en `runtime/config-panel-*.yaml`. El bot debe estar
detenido. Los cambios se aplican al próximo arranque. Los IDs de planeta se ven
en los informes del scanner; las listas de IDs se separan con comas.

La cuenta, rutas del perfil y credenciales no se editan aquí. Telegram usa las
variables de entorno ya configuradas en el bot.

## Modos y recursos reales

El selector permite todos los módulos habilitados, solo scanner, solo sesión
o solo defensor. Solo scanner lee sin ejecutar mejoras ni defensa.

`automation.dry_run` permanece activo para las flotas desde este panel. La opción
`automation.economic_live` activa explícitamente edificios e investigación
con recursos reales según sus objetivos, manteniendo las flotas en simulación.
Está desactivada por defecto y no habilita fabricación general de naves.

La preparación de sonda tiene su propia opción real limitada. Mientras esté
seleccionada, pausa las prioridades normales incluso después de completarse;
desactívala en Hangar para probar el constructor habitual. Ambos modos reales
respetan comprobaciones de identidad, recursos, amenazas, colas y registro.

## Parada

El bot actualizado registra su control tras adquirir el bloqueo del perfil.
La parada funciona también para un bot abierto mediante BAT o después de
reiniciar el panel. Se comprueba PID y fecha de creación del proceso, y se envía
una solicitud con token. El bot termina en un punto de control al finalizar la
operación actual; no se mata ningún proceso del sistema.

Una instancia antigua ya en ejecución necesita detenerse una vez con Ctrl+C
y volver a abrirse con el código actualizado. El panel lo indicará si la detecta.
Cerrar el servicio del panel deja el bot activo; usa Detener bot primero si
quieres cerrar ambos. Las lecturas antiguas quedan identificadas como tales.

El panel escucha solo en loopback, comprueba Host/Origin y exige token para
cambios. No habilites reenvíos de puerto. Los envíos reales, evacuación completa
y funcionamiento prolongado en el juego continúan pendientes de validación.
