# Estado del defensor

El modo de simulación sigue controlado por `automation.dry_run: true`.
No desactivarlo hasta validar el recorrido de flota y su confirmación.

El informe `runtime/defender_state.json` separa el ataque de la reacción:

| Campo | Significado |
| --- | --- |
| `state: detected` | Hay un indicador o evento de amenaza. |
| `threats` | Destino identificado, llegada y tiempo restante por evento. |
| `destination_checks` | Destinos configurados que pertenecen a la cuenta y no tienen amenazas conocidas. No garantiza que un destino sea seguro en el futuro. |
| `result.state: waiting_window` | La amenaza todavía está fuera del umbral `defender.trigger_seconds`. |
| `result.state: blocked` | No se cumplen los requisitos; `reason` identifica el bloqueo. |
| `result.state: simulated` | Se preparó una propuesta sin enviar ninguna flota. |

La comprobación de destinos se realiza también antes de entrar en la ventana
de evacuación. Los motivos principales son:

- `no_other_own_body`: solo hay un cuerpo propio; despliegue o transporte propio requieren otro planeta o luna.
- `destinations_not_configured`: hay otros cuerpos, pero falta indicar sus identificadores en `defender.destinations`.
- `no_verified_own_destination_outside_known_threats`: los identificadores no son propios, coinciden con el origen o apuntan a blancos de amenazas conocidas.

No se sustituyen destinos ausentes por coordenadas de otros jugadores. No se
selecciona automáticamente una misión de ataque, colonización o reciclaje.

## Espionaje a posición 16

La opción explícita `defender.mission: espionage16` utiliza misión 6 a posición
16 del mismo sistema que el origen, al 10% de velocidad. No requiere otro
planeta ni slots de expedición. Requiere una sonda y al menos una carguera,
además del inventario completo de naves móviles que se evacuarán. Permite
cargar recursos según la capacidad verificada. No espera una expedición y
no programa una cancelación: el recorrido es de ida y vuelta.

En este modo `minimum_flight_seconds` es la duración mínima de ida y vuelta;
en despliegue y transporte sigue siendo el mínimo del tramo de ida. El
ejecutor comprueba la duración real mostrada por el formulario y exige que
el regreso quede después del impacto con el margen `minimum_lead_seconds`.
Las simulaciones se etiquetan `timing_verified: false`: no verifican el
presupuesto de vuelo del servidor ni prueban el envío real.

La posibilidad del envío con sonda, cargueras y recursos a posición 16 figura
en la directiva reproducida en el [foro oficial](https://forum.es.ogame.gameforge.com/forum/thread/32509-no-consigo-completar-la-misi%C3%B3n-directiva-iii-despeja-una-flota-con-recursos/).

El scanner guarda `fleet_slots` desde la primera página de flota leída. En la
interfaz española se leen las etiquetas `Flotas` y `Expediciones` dentro de
`#slots`, ignorando el segundo panel homónimo de retirada táctica. Lecturas
ambiguas o inconsistentes permanecen desconocidas; un contador `0/0` es válido.

Antes de permitir envíos también hacen falta inventario completo, capacidades
efectivas por nave, un presupuesto de combustible compatible con los recursos,
un slot libre y la validación del trayecto. Un destino configurado no elimina
esos requisitos ni equivale a una evacuación probada.

## Diagnóstico sin navegador

`Diagnostico_bot.bat` o `python -m ogame_bot --diagnose` muestra los bloqueos
de configuración, módulos desactivados y las últimas lecturas vigentes. No
abre el navegador, no se conecta a Gameforge y no envía órdenes. Tampoco
modifica la configuración ni el estado guardado.

Una instantánea antigua no demuestra que falte una nave: se solicita una
lectura nueva. Las instantáneas de otra identidad o servidor se descartan.
El diagnóstico siempre señala que el envío real y el funcionamiento
prolongado siguen pendientes; una prueba de lógica no los certifica.
