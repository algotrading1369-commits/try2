# OGame Bot — proyecto modular Python

Energía por satélites, prioridad de almacenes, flotas y recuperación:
[guía de configuración y límites de validación](ENERGIA_FLOTAS_ESTABILIDAD.md).

Implementación independiente del prototipo de la carpeta superior. Python 3.11+.
No comparte su perfil ni cambia los archivos de estado del prototipo anterior.
Mejoras y envíos parten en simulación. El panel permite habilitar por separado
economía, fabricación y expediciones reales, conservando la simulación general
para las demás misiones. Véase [Hangar y Expediciones](HANGAR_EXPEDICIONES.md).
La excepción explícita `probe_bootstrap_live: true` habilita solo los requisitos
y una sonda, manteniendo las flotas en simulación. Véase [SONDA.md](SONDA.md).
El acceso inicial y la entrada desde el Lobby al universo
son manuales; la recuperación reutiliza las cookies del perfil dedicado.

El repositorio reúne los seis módulos, incluido el scheduler general. La interfaz real aún no se ha validado; las pruebas
actuales usan lógica, HTML y formularios sintéticos.

## Instalación desde un clon

En Windows ejecuta Instalar_windows.bat o, desde la carpeta del repositorio:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item config.example.yaml config.yaml
.\.venv\Scripts\python.exe -m ogame_bot --check-config
```

Configura tu universo y expected_player_name en config.yaml. La plantilla usa
un jugador ficticio, Test Commander. El acceso inicial es manual. Los accesos
.bat prefieren .venv local y admiten también el entorno de la carpeta superior
para la instalación anterior.

config.yaml está excluido de Git, junto con runtime, sesiones, logs, bases de
datos y archivos .env. La plantilla y las pruebas solo contienen ejemplos.
El flujo de GitHub compila, valida configuración y ejecuta pruebas de lógica;
no inicia sesión ni necesita credenciales.

## Arranque en Windows

En este equipo puedes abrir directamente `Abrir_modulo_1.bat`.
En otra instalación, prepara antes las dependencias con los comandos siguientes.

Desde la carpeta OGame, en PowerShell:

```powershell
.\.venv\Scripts\python.exe -m pip install -r .\modulo1\requirements.txt
Set-Location .\modulo1
..\.venv\Scripts\python.exe -m ogame_bot --check-config
..\.venv\Scripts\python.exe -m ogame_bot
```

Comprueba comunidad, servidor y expected_player_name en config.yaml antes del
primer arranque. El archivo local puede conservar los ajustes del prototipo.

Si el perfil no tiene sesión válida, entra manualmente en el navegador abierto
y selecciona el universo. El núcleo reconoce la nueva pestaña por el servidor
exacto y verifica #playerName. No se consideran autenticadas otras comunidades
ni universos. Una identidad distinta detiene el proceso.

Con Chrome instalado no hace falta descargar Chromium. Para usar el Chromium
de Playwright, configura channel: null y ejecuta antes:

```powershell
..\.venv\Scripts\python.exe -m playwright install chromium
```

Cierra el navegador o pulsa Ctrl+C para terminar. No abras manualmente el
perfil dedicado con otro Chrome mientras esté funcionando el núcleo.

## Comportamiento y límites

- Cookies y almacenamiento del navegador persisten en runtime/browser-profile.
- El perfil tiene bloqueo de proceso compatible con Windows y Linux.
- Las operaciones de sesión usan un bloqueo asíncrono exclusivo.
- Cada comprobación recarga overview para detectar una sesión caducada; no basta
  con conservar el nombre del jugador en un DOM antiguo.
- Los errores de red tienen intentos y backoff limitados con jitter.
- La pérdida de autenticación requiere intervención; en headless se detiene.
- No se automatizan el formulario del Lobby ni desafíos interactivos.
- No se promete evasión de detección; el jitter solo espacia operaciones.
- El núcleo controla su pestaña de trabajo: no la uses para jugar manualmente.
- No reinicia el navegador al cerrarlo el operador.
- Los logs rotan y no registran cookies, contraseñas, URLs ni errores originales
  de Playwright. No se generan capturas de la cuenta.

Los módulos 2–6 se
describen a continuación.

## Módulo 2: Scanner / State Reader

Abre `Abrir_scanner.bat` para ejecutar sesión y lecturas periódicas. El acceso
inicial sigue siendo manual. `Abrir_modulo_1.bat` conserva el modo de solo sesión.
El núcleo permanece en esta carpeta para compartir configuración y perfil.

Desde PowerShell dentro de modulo1:

```powershell
..\.venv\Scripts\python.exe -m ogame_bot --scan-once
```

Esta opción conecta, realiza una lectura y cierra el navegador. Sin
`--scan-once`, el intervalo de 120–240 segundos se cuenta desde que termina
cada recorrido; el recorrido tiene su propia duración.

La sección `scanner` de config.yaml permite desactivar el módulo, cambiar la
salida, los intervalos y el máximo de cuerpos recorridos por ciclo. Por defecto
la salida es runtime/state.json, independiente de estado_juego.json del
prototipo anterior.

### Datos y cobertura

| Sección | Contenido |
| --- | --- |
| registry | Planetas y lunas con identificador propio y coordenadas. |
| bodies[].resources | Metal, cristal, deuterio y balance de energía. |
| bodies[].buildings | Minas, fuentes de energía y almacenes reconocidos. |
| bodies[].facilities | Instalaciones del planeta o de la luna. |
| bodies[].ships | Naves disponibles en fleetdispatch; no cantidades introducidas en formularios. |
| research | Investigación leída en un planeta. |
| movements | Indicador global y eventos reconocidos, con misión, retorno y llegada UTC. |

Cada sección tiene `observed_at`, `valid_until`, `status` e `issues`. Los estados
son `complete`, `partial`, `unavailable` o `not_applicable`. Complete indica
cobertura del contrato de lectura, no certificación del frontend actual.
La fecha finished_at es la finalización del recorrido, no la fecha de todas
las lecturas. Los datos caducan individualmente según max_reading_age_seconds.
El helper Reading.is_fresh() permite comprobar su vigencia en los próximos
módulos. Un consumidor debe verificar también cobertura y estado antes de actuar.

Se distingue cero confirmado de un valor ausente. No se convierten abreviaturas
como 1,2 M en valores exactos. Las secciones que fallan no heredan datos ni fechas
de un ciclo anterior. La escritura JSON es atómica y no conserva HTML, cookies,
enlaces con tokens ni scripts de la página.

El cuerpo seleccionado se verifica con meta ogame-planet-id antes de asignar
recursos o inventario. No se asume que cp en la URL garantice que el servidor
haya seleccionado ese cuerpo.

### Ataques

El scanner intenta abrir el panel global de eventos con eventboxFilled y lee
las filas eventFleet y el indicador attack_alert. Los marcadores de misión,
hostilidad y retorno deben ser explícitos. La llegada se publica cuando existe
un timestamp exacto en segundos; no se infiere de un reloj local.

threat_status puede ser detected, clear o unknown. Si no hay evidencia suficiente,
la lectura de movements es unavailable con data null; esto también significa
desconocido. Un panel vacío no confirma ausencia de ataques. Un indicador
noAttack sin listado permite informar clear, pero la cobertura sigue siendo
partial. Los eventos incompletos impiden declarar clear.

El scanner por sí solo genera un log crítico local; las alertas y evacuación
corresponden al Defender del módulo 5. El recorrido de inventario puede durar varios minutos con muchos
planetas; esta versión no es un monitor defensivo de baja latencia.

### Límites de esta entrega

Los selectores del adaptador están implementados y probados con HTML sintético.
La parte de edificios aprovecha el contrato observado en el prototipo anterior.
Los selectores de planetas, flotas y eventos necesitan validación en la interfaz
real actual. Un formato no reconocido debe producir partial/unavailable, nunca
un inventario inventado. No se ha accedido a la cuenta durante esta entrega.

## Módulo 3: Auto-Builder & Research

El módulo se integra en el núcleo de esta carpeta. Los accesos disponibles son:

| Acceso | Comportamiento |
| --- | --- |
| Abrir_modulo_1.bat | Solo sesión, sin scanner ni Brain. |
| Abrir_scanner.bat | Lecturas periódicas, sin evaluar ni enviar mejoras. |
| Evaluar_brain.bat | Una lectura y una evaluación del Brain; respeta dry_run. |
| Abrir_bot.bat | Sesión, lecturas y evaluación periódica del Brain. |

La configuración entregada usa `automation.dry_run: true`. Por tanto el Brain
solo genera propuestas y no pulsa botones de mejora. Guarda su informe en
runtime/plan.json, con acciones, motivos de espera y resultado de la evaluación.

### Configuración

- builder.priorities es una lista ordenada de item y target_level. Se aplica a
  todos los planetas verificados. No se construye en lunas en esta entrega.
- strategy: priority completa el primer objetivo pendiente sin saltar niveles
  desconocidos ni objetivos que aún no se puedan pagar.
- strategy: roi compara el siguiente nivel de las minas configuradas hasta sus
  objetivos. Requiere economy_speed y la temperatura máxima por identificador
  de planeta cuando se compara deuterio.
- production_multipliers debe incluir el efecto total relevante de posición,
  clase, Plasma, objetos y otras bonificaciones de cada recurso. El valor 1
  representa el modelo base, sin esos efectos. No se deducen de la cuenta.
- resource_weights expresa equivalentes de metal: los valores entregados
  corresponden al ratio metal:cristal:deuterio de 2,5:1,5:1.
- reserve es el mínimo que debe quedar después de pagar una mejora. La reserva
  de deuterio entregada es 100.000: ajústala al tamaño de tu cuenta.
- minimum_energy es el margen energético requerido. auto_solar permite proponer
  una planta solar antes de una mina que no tendría energía suficiente.
- research.priorities ordena las tecnologías objetivo. El botón de OGame debe
  estar disponible: el módulo no inserta prerrequisitos que no hayas configurado.
- automation.min_interval_seconds/max_interval_seconds espacian evaluaciones,
  independientemente del intervalo de lectura. Los cambios requieren reiniciar.

Para ROI, un ejemplo de temperatura es `temperatures: {101: 40}`: 101 es el
identificador del planeta en registry y 40 su temperatura máxima en °C.
No uses ese identificador salvo que corresponda a tu cuenta. economy_speed se
deja en null porque la velocidad del servidor aún no se ha leído ni verificado.

Los costes son estimaciones del modelo clásico sin descuentos, no precios
extraídos de la interfaz. Astrofísica usa un redondeo superior a centenas para
la comprobación de presupuesto. El ROI añade el coste solar modelado necesario
para alimentar la siguiente mina. Si hace falta más de un nivel solar, el módulo
solo propone el primero y vuelve a medir la energía antes de seguir.

El ROI supone minas al 100% y producción marginal constante. No incluye tiempo
de construcción, cambios futuros de bonificaciones, fusión, taladradores ni
satélites. No calcula la amortización de investigaciones; estas usan prioridades.

El catálogo implementado incluye las tres minas, solar, los tres almacenes,
robots, nanobots, hangar, laboratorio y silo, además de las investigaciones
clásicas excepto Gravitón. Los elementos no soportados se rechazan al validar
el YAML, incluidos edificios de formas de vida y edificios con costes especiales.

### Ejecución y recuperación

Al configurar dry_run: false, una evaluación puede enviar como máximo una mejora.
Antes del clic comprueba servidor y jugador, planeta y coordenadas, nivel actual,
recursos, reservas, energía, indicador de ataques, cola explícitamente libre,
tarjeta identificada y botón exacto disponible. Una lectura desconocida bloquea
el envío. Los requisitos efectivos del juego se comprueban con la disponibilidad
del botón; no se fuerzan botones desactivados.

runtime/orders.sqlite3 se vincula a cuenta y servidor y guarda la orden antes del
clic. Una orden prepared, submitted o uncertain bloquea los envíos de su categoría.
El registro sobrevive a reinicios. Solo se confirma automáticamente cuando una
lectura verifica que el nivel objetivo ya se ha alcanzado. Una cola que parezca
libre o un clic que devuelva sin error no prueban que la mejora haya terminado.
Si el clic no llegó a ejecutarse pero no se puede demostrar, el registro sigue
bloqueado; el módulo no vuelve a pulsar a ciegas ni borra el registro para reintentar.

Cuando ambas clases de mejora tienen propuestas viables, se alternan edificio
e investigación según la última orden confirmada; reparar un déficit de energía
tiene prioridad. No hay envíos simultáneos de edificios e investigación.

Las colas se reconocen solo con mensajes o contadores explícitos del panel de
producción. Si ese panel cambia de formato, el módulo espera y lo indica en el
informe. Los selectores de envío y las fórmulas siguen pendientes de validación
en la interfaz real actual; esta entrega se ha probado con simulaciones.

## Módulo 4: Fleet & Expedition Manager

Evaluar_flotas.bat escanea y evalúa una vez, sin evaluar edificios. Abrir_bot.bat
incluye los módulos 1–4. Ambos respetan automation.dry_run, que sigue siendo true.
Los tres tipos de misión parten desactivados y sin orígenes: deben configurarse
con los identificadores que publique registry. runtime/fleet_plan.json contiene
las alternativas, los datos que faltan y el resultado.

### Expediciones

La composición apunta a recursos: cubre tanto la carga modelada como los puntos
de expedición del modelo histórico. Permite un explorador, una sonda y una nave
de combate configurada; no pretende optimizar combates, hallazgos de naves o MO.
El margen de carga incluye maximum_fuel, una cota conservadora configurable.

top1_source: public_api consulta highscore.xml del propio servidor y conserva
el timestamp de la exportación. La caché evita consultar en cada navegación.
También admite top1_source: manual con top1_points y top1_observed_at, fecha ISO
con zona horaria. Un dato caducado o de otro servidor no se usa para planificar.

Debes configurar economy_speed, clase y capacities con capacidades efectivas
verificadas de las naves de carga. No se deducen bonificaciones desconocidas.
resource_bonus_percent representa el bonus adicional de recursos de expedición.
El modelo combina clase, velocidad económica, explorador y ese bonus. La tabla
histórica del top 1 es configurable mediante base_cap_override; por debajo de
10.000 puntos exige override. No se afirma que esa tabla describa todas las
variantes actuales del juego. Fuentes de referencia:

- https://board.en.ogame.gameforge.com/index.php?thread/849973-expedition-questions/
- https://board.en.ogame.gameforge.com/index.php?thread/814096-discoverer-info/&pageNo=2
- https://forum.origin.ogame.gameforge.com/forum/thread/551-ogame-empire-tracker-1-0-0/

### Transporte propio y reciclaje

Los transportes llevan recursos desde sources al destination_id propio; no se
aceptan coordenadas arbitrarias de terceros. Respetan leave_resources, reserva
de deuterio, capacidad y utilización mínima. Se calculan con margen de combustible.
Este transporte no constituye una garantía de fleetsave seguro: el módulo de
defensa se describe en la sección del módulo 5.

El reciclaje recorre sistemas cercanos a los orígenes dentro del radio y del
límite max_systems_per_cycle. Verifica la galaxia y sistema de cada lectura,
elige campos con tamaño suficiente y propone recicladores con capacidad para
el campo. No asume que la ausencia de datos signifique cero escombros. Antes
de enviar vuelve a comprobar el campo; puede desaparecer mientras la flota vuela.

### Envío y registro

El contrato de selectors está en YAML. La implementación rellena el formulario
en tres pasos, lee de vuelta composición, destino, velocidad, misión, permanencia
y carga, y comprueba combustible, capacidad y duración publicados por la interfaz.
Limpia selecciones previas y rechaza naves adicionales. No calcula combustible
con fórmulas incompletas ni pulsa el botón final si no puede verificar esos datos.
El contrato de velocidad de esta versión usa opciones porcentuales 10–100.

El parser de movimientos requiere un contenedor fleetMovements marcado como
cargado y filas con identidad, misión, origen, destino, naves y carga explícitos.
La galaxia requiere galaxyContent con sistema y cantidades de escombros exactos.
Estos contratos se prueban con HTML sintético; no se han confirmado en el frontend
actual. Un formato distinto bloquea la función y requiere adaptar el lector o
los selectores. No se presenta esta entrega como operativa en una cuenta real.

Cada evaluación envía como máximo una de las alternativas y registra la orden
antes del clic. Se confirma únicamente al identificar una nueva flota propia
que coincida exactamente y cuyo ID no estuviera en la lectura anterior. Un
resultado ambiguo queda pendiente y no se reenvía, incluso tras reiniciar.
Si la flota ya ha regresado antes de poder observarla, puede quedar pendiente:
el programa no interpreta su ausencia como prueba de que nunca salió.

La base de órdenes permite una orden pendiente de mejora y otra de flota. Así
una investigación larga no bloquea expediciones. Los clics siguen serializados
y cada envío vuelve a comprobar recursos actuales. No uses la pestaña de trabajo
ni ejecutes envíos manuales paralelos mientras se verifica una transacción.

## Módulo 5: Defender / Auto-Fleetsave

Abrir_defensa.bat ejecuta solo el monitor. Evaluar_defensa.bat comprueba una vez.
Abrir_bot.bat añade el monitor a las tareas de los módulos 1–4. Los accesos de
solo scanner, sesión, Brain y flotas mantienen su alcance sin evacuar por sorpresa.

El Defender viene habilitado en simulación, sin destinos y con Telegram apagado.
Antes de poder planificar una evacuación debes indicar IDs propios en
defender.destinations y capacidades efectivas en fleet.capacities. Una lista de
origins vacía protege todos los cuerpos reconocidos; una lista no vacía limita
los cuerpos desde los que se puede evacuar.

### Detección y plan

El monitor lee el resumen y los eventos sin recorrer todas las minas. Distingue
planeta y luna por ID o tipo explícito de destino; una coordenada sola no basta.
El adaptador reconoce data-destination-id y data-destination-type en las filas
de eventos. No inventa un destino, hora de impacto o dirección ausentes.

Con los valores entregados, la ventana de evacuación empieza a 600 segundos del
impacto y exige más de 30 segundos para preparar la salida. Las lecturas caducan
a los 20 segundos. Un ataque fuera de ventana se anuncia y se sigue observando;
no se espera al último instante si ya entra en la ventana configurada.

El plan selecciona todas las naves móviles cuyo inventario se pueda confirmar,
incluidas las cantidades cero del catálogo. Un tipo desconocido o un recuento
ausente bloquea el envío para no abandonar una parte de la flota inadvertidamente.
Satélites y taladradores no son naves evacuables. Las capacidades deben incluir
los tipos con cantidades positivas; se admite capacidad cero para una sonda.

Se elige el primer destino propio configurado que no sea el origen y no aparezca
bajo otra amenaza conocida. La lista de destinos es una decisión del operador;
la mera propiedad del cuerpo no garantiza que la ruta esté libre de riesgos.
La misión predeterminada es despliegue; también admite transporte explícito.

require_all exige capacidad para los recursos. maximize_value lleva todos los
barcos y prioriza deuterio, cristal y metal según resource_weights. El plan inicial
reserva maximum_fuel como cota de combustible. En el formulario se relee el
combustible real y se ajusta la carga a la capacidad real antes del registro.
La orden persistida contiene esa carga final, publicada en result.effective_action.
maximum_fuel es configurable y una cota demasiado alta puede impedir elaborar
el plan inicial en una cuenta pequeña.

### Condiciones de envío

La defensa puede consumir el último hueco reservado de flota. Antes del clic
final verifica de nuevo la identidad, blanco, evento, llegada, naves, recursos,
capacidad, combustible y duración. El vuelo debe superar minimum_flight_seconds
y el tiempo restante hasta el impacto más el margen, sin superar maximum_flight_seconds.
No calcula ni presupone la duración: la obtiene del contrato de la interfaz.
Si el ataque cambia, desaparece, queda demasiado poco tiempo o falta un dato,
no se envía la evacuación y se registra el motivo.

Las evacuaciones usan la misma categoría persistente de órdenes de flota que
el módulo 4. Si ya existe un envío sin confirmar, se intenta reconciliarlo y se
avisa del bloqueo; no se borra el registro ni se envía una segunda flota a ciegas.
La confirmación requiere identificar un movimiento saliente nuevo que coincida.
El Defender no cancela flotas en vuelo ni fabrica cargueros para una emergencia.

### Monitor, pausas y errores

Sin amenazas conocidas revisa cada 120–240 segundos; con amenaza o estado
desconocido, cada 10–20 segundos. Los intervalos cuentan desde que termina cada
comprobación. Comparte el bloqueo del navegador con las otras tareas y no puede
interrumpir una navegación que ya esté ejecutándose.

Una amenaza pausa nuevas mejoras y envíos ordinarios. Por defecto también se
pausan cuando el estado de ataques sea desconocido. Una lectura clear libera
la pausa. Un fallo inesperado del monitor termina el bucle ordinario, para que
no continúe sin monitor activo. La planificación general se describe en el módulo 6.

runtime/defender_state.json registra fecha, amenazas, pausa, propuesta y resultado.
Con solo el indicador global de ataque, un blanco ambiguo, combustible insuficiente,
sin hueco, sin destino o un envío incierto, el resultado es alerta y bloqueo.

Estos intervalos y la latencia de la página dejan ventanas sin observar: esta
entrega no garantiza detectar ataques más rápidos ni protección 24/7. Los
selectores y las transacciones siguen pendientes de pruebas en el juego real.

### Telegram

El envío usa la [Bot API de Telegram](https://core.telegram.org/bots/api#sendmessage).
Crea un bot, inicia su chat y configura las variables de entorno
TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID de tu propio chat. Activa telegram.enabled
solo cuando quieras recibir las alertas. No guardes el token en YAML ni en Git.

Las alertas se procesan en segundo plano y no retrasan la evacuación esperando
la red. Se anuncian detección, falta de datos, bloqueo y resultado. La simulación
se etiqueta explícitamente como tal: dry_run no desactiva Telegram si lo has
habilitado. En esta entrega no se ha enviado ningún mensaje real.

Hay timeout, deduplicación de entregas correctas y cooldown persistente en
runtime/alert_state.json. Una entrega fallida puede reintentarse en otra revisión.
No se registran URL de autenticación, token ni respuesta cruda del proveedor.
El historial contiene hashes y fechas, no textos ni credenciales.

## Módulo 6: Scheduler

Los modos continuos pasan por un único scheduler: Abrir_bot.bat, Abrir_scanner.bat,
Abrir_defensa.bat y el modo de solo sesión. Los accesos Evaluar_* siguen siendo
ejecuciones de una sola evaluación, con el mismo dry_run y las mismas verificaciones.
No hay un segundo bucle de defensa compitiendo con el recorrido del scanner.

### Trabajos y dependencias

| Trabajo | Prioridad predeterminada | Intervalo |
| --- | --- | --- |
| Defender | 0 | Los intervalos idle/alert del módulo 5. |
| Sesión | 10 | session.health_check_seconds. |
| Reconciliación | 20 | scheduler.reconciliation_min/max_seconds. |
| Scanner | 30 | scanner.min/max_interval_seconds. |
| Brain | 40 | automation.min/max_interval_seconds. |
| Flotas | 50 | fleet.min/max_interval_seconds. |

Un número menor tiene mayor prioridad. Defender y sesión deben preceder al resto;
el scanner debe preceder a Brain y flotas. Los valores se validan al cargar YAML.
Cada trabajo calcula su próximo plazo con reloj monotónico y jitter dentro de
su intervalo, desde que termina la ejecución. No se acumulan ejecuciones atrasadas
ni se lanzan varias instancias del mismo trabajo.

El scheduler comprueba los trabajos urgentes antes de cada navegación, fuera
del bloqueo del navegador. Esto permite insertar una revisión de defensa entre
dos páginas de un scanner largo. No interrumpe un clic, un formulario de envío
ni una navegación que ya está en curso. El acceso al navegador sigue serializado.

Brain y flotas esperan una instantánea inicial y una sesión válida. Si el scanner
está demasiado atrasado, pueden pedir una nueva lectura como dependencia antes
de planificar. La vigencia de cada sección sigue comprobándose por separado:
un recorrido terminado no convierte en completos o actuales todos sus datos.

Una pausa defensiva bloquea trabajos económicos; las lecturas de defensa y
reconciliación siguen disponibles. Una recuperación de autenticación obliga a
revisar la defensa antes de reanudar esos trabajos. Una comprobación de sesión
normal no cambia los intervalos configurados del Defender.

### Errores y recuperación

ReadError, LoginRequired y ConnectionLost pueden reintentar la tarea con backoff
acotado y contador de fallos consecutivos. El valor entregado es tres fallos.
Una recuperación correcta reinicia el contador. La espera por datos no consume
ese presupuesto. Un fallo crítico del Defender o un error inesperado detiene
el scheduler completo; no se mantiene una economía funcionando sin monitor.

Estos reintentos vuelven a evaluar el servicio y su registro de órdenes. Nunca
repiten directamente un clic ni borran órdenes inciertas. Las confirmaciones son
idempotentes para que una revisión urgente y otra periódica puedan acreditar la
misma orden sin crear un fallo ni una nueva transacción.

No hay timeout que cancele una transacción de juego a mitad. Las operaciones
de navegador y red conservan sus límites propios. Ctrl+C cancela el bucle y cierra
notificaciones, registro y navegador. Una cancelación durante el clic conserva
el estado incierto para reconciliarlo en el siguiente arranque.

Al reiniciar se inicia una lectura y comprobación nuevas. No se restaura una
lista de clics atrasados desde el archivo de estado. El registro SQLite sí conserva
las órdenes pendientes, que siguen bloqueando su categoría hasta verificarse.

### Estado y diagnóstico

Ver_estado.bat, o `python -m ogame_bot --scheduler-status`, muestra el último
runtime/scheduler_state.json sin abrir navegador ni iniciar sesión. Incluye:

- Estado created/running/stopped/failed y hora del último heartbeat.
- Vigencia del heartbeat y resultado heartbeat_is_recent al consultarlo.
- Trabajos activos, prioridad, próxima ejecución y motivo de bloqueo.
- Ejecuciones, fallos, aplazamientos, duración y resultado de cada trabajo.

El heartbeat se guarda cada heartbeat_seconds cuando el bucle tiene control y
al empezar/terminar un trabajo. Puede caducar durante una operación larga; no es
una prueba independiente de que el proceso haya muerto. Un heartbeat reciente
con state: stopped indica un cierre reciente, no un proceso todavía activo.

Las fechas de diagnóstico son UTC. Los plazos internos no dependen de cambios
del reloj de Windows. El estado no incluye excepciones crudas, URLs de sesión
ni credenciales. El archivo es privado y queda excluido de Git.

### Alcance actual

Los seis módulos están implementados e integrados en el repositorio. La validación
actual es de lógica, HTML y formularios simulados. Siguen pendientes las pruebas
con el frontend real y la ejecución prolongada en un VPS. Un scheduler operativo
en pruebas no acredita protección real ni funcionamiento estable 24/7 del juego.

## Validación con la interfaz real

Prueba_lectura_60min.bat inicia una prueba de lectura de 60 minutos, contados
después de iniciar sesión. Cierra y reabre únicamente el navegador del bot cada
15 minutos para comprobar la persistencia. No controla ni cierra otras ventanas
de Chrome. El primer acceso puede requerir que entres manualmente en tu cuenta.

Antes de abrir el navegador hace una comprobación aislada de asyncio con un
timeout externo. Así un bloqueo al crear el bucle de Windows produce un informe
en lugar de aparentar que el bot está trabajando.

La prueba usa Scanner, SessionManager y Scheduler reales, pero no instancia los
servicios de construcción, envío o Telegram. Fuerza simulación y guarda todos
sus resultados en runtime/validation, en una subcarpeta por ejecución. No guarda
HTML, tokens o cookies en el informe. El perfil de sesión mantiene su privacidad
y bloqueo habituales. No inicies otro controlador con ese perfil durante la prueba.

Desde la carpeta del repositorio también admite:

```powershell
.\.venv\Scripts\python.exe -m ogame_bot.validation --minutes 60 --restart-minutes 15
.\.venv\Scripts\python.exe -m ogame_bot.validation --probe-only
```

El informe report.json separa duración alcanzada, lecturas completas/parciales,
errores, controles observados y reinicios correctos de sesión. Los pasos posteriores
del formulario de flota y los envíos reales quedan explícitamente sin probar.
Completar el tiempo solicitado no significa que todos los selectores sean compatibles.
Los cierres y la operación en curso pueden añadir latencia al plazo de observación.

El intento desde el entorno de desarrollo se bloqueó antes de abrir navegador:
asyncio quedó esperando en socket.accept dentro de _fallback_socketpair. Ese
resultado no valida la interfaz ni una sesión prolongada del juego. Tampoco se
pudo inspeccionar la ventana existente de Chrome: la autorización de control caducó.

## Cambio de cuenta y registro de órdenes

El registro se abre después de verificar la sesión y el nick del juego. Las nuevas
instalaciones generan un archivo SQLite con sufijo por cuenta/servidor, a partir
de automation.journal_file. Al volver a esa configuración se recupera el mismo
registro, incluidas sus órdenes pendientes.

Si una versión anterior creó orders.sqlite3 con el nick de ejemplo antes de
iniciar sesión, el nuevo arranque lo conserva y utiliza un archivo independiente
para la cuenta correcta. No hace falta borrar la carpeta runtime ni la sesión.
Un registro heredado que coincida con la identidad configurada se sigue utilizando.

Si el registro heredado pertenece a otra identidad y contiene órdenes inciertas,
se detiene y pide revisar esas órdenes. No se cambia su propietario ni se borra
su contenido para eludir el bloqueo. Una cuenta de prueba debe usar una ruta de
perfil distinta de la cuenta principal y conservar dry_run durante la validación.

## Pruebas locales sin cuenta

### Continuidad de construcción e investigación

El constructor y la investigación usan registros pendientes independientes: una
investigación no bloquea la cola de edificios. Cada envío se registra antes del
clic, se vuelve a leer la economía y se evalúa la otra cola con los recursos
restantes. Al finalizar una mejora, se confirma su nivel antes de decidir la
siguiente. Cambiar entre prioridad y ROI en el panel solicita una lectura nueva
sin reiniciar la sesión. Los registros anteriores se migran sin borrar órdenes.

El panel admite ratios de intercambio, por ejemplo 2.5:1.5:1. El YAML mantiene
`resource_weights` como valores relativos: el ejemplo equivale a metal=1,
cristal=1.6666666667 y deuterio=2.5. La temperatura puede leerse de un rango
explícito en Visión general o fijarse manualmente. Si falta, ROI excluye el
deuterio y sigue comparando las minas verificadas, indicando la limitación.
Si la solar automática está desactivada y falta energía, se muestra el bloqueo.

La prueba `test_autonomous_economy.py` encadena ocho órdenes por estrategia con
juego simulado y planificador, ejecutor, journal y scheduler reales; verifica
recursos restantes, colas independientes y ausencia de navegación durante las
esperas. No sustituye una prueba prolongada en el servidor real.

```powershell
..\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Las pruebas de navegador interceptan todas las peticiones: no contactan con
Gameforge y usan HTML y cookies sintéticos en perfiles temporales.
Para incluirlas, activa primero `$env:OGAME_BROWSER_TESTS = '1'` en PowerShell.
Por defecto solo se ejecutan las pruebas de lógica, que no abren Chrome.

## Verificación de esta entrega

En la entrega del núcleo se superaron 12 pruebas de configuración y lógica.
Las pruebas nuevas del scanner cubren análisis de HTML, amenazas, separación de
planetas, caducidad y persistencia. Las 7 pruebas con Chrome no se han ejecutado.
Las pruebas del Brain cubren prioridades, energía, ROI modelado, reservas,
configuración, colas, registro antes del clic, envío único, reinicios y simulación.
Las de flotas cubren composición, top 1, slots, transporte propio, escombros,
combustible, lectura de vuelta, envío registrado y confirmación sin duplicados.
El entorno de desarrollo bloquea incluso la creación de
un bucle asyncio normal. Para las pruebas de lógica se utilizó un bucle de
pruebas sin E/S, manteniendo las tareas, cancelación y bloqueos de asyncio.
Este runner está disponible para reproducir la verificación:

```powershell
..\.venv\Scripts\python.exe tests\run_without_io.py
```

La configuración también se validó con la entrada real --check-config.
La capacidad de almacenes y los campos del planeta se contrastaron con el DOM
real de ES 13.1.0-r5: `data-tooltip-title` contiene una tabla con la capacidad;
`diameterContentField` contiene los campos usados/totales. El scanner espera el
detalle numérico de Visión general antes de capturar el HTML, porque ese panel
se carga después del marco autenticado. Las pruebas de regresión reproducen
ese formato y comprueban la prioridad del almacén al alcanzar el umbral tanto
en ROI como en cola. No se ejecutó una construcción real para esta validación.
Tras caducar el juego, el bot intenta reutilizar el acceso vigente al Lobby:
abre la única fila que coincida exactamente con jugador y comunidad, y solo
reanuda tras verificar el dominio del universo y la identidad dentro del juego.
Si hay varias coincidencias, el Lobby pide autenticación o el juego no confirma
el acceso en 30 segundos, espera acceso manual. Este flujo tiene pruebas con
interfaces simuladas; aún falta comprobar una expulsión nocturna real.

La recuperación espera hasta 15 segundos a que se rendericen el enlace Jugar y
la tabla de cuentas del Lobby. Si el primer intento no encuentra la interfaz,
se mantienen los reintentos durante la ventana de acceso, separados por 15
segundos, en lugar de esperar exclusivamente una entrada manual. El registro
indica qué paso falta mediante `Reconexión del Lobby pendiente`. Una caducidad
que conserva el dominio del juego también solicita reconexión cuando no se
puede verificar la identidad. Tras recuperar el acceso, se programa una nueva
lectura completa y se comprueba defensa antes de continuar la automatización.
Durante la ejecución continua, un intento fallido de reconexión devuelve el
control al scheduler para programar el siguiente intento y actualizar su estado;
no retiene el bloqueo del navegador esperando diez minutos de acceso manual.

### Inicio automático cuando caduca el Lobby (Windows)

Ejecuta `Configurar_acceso.bat` una vez e introduce localmente tu correo y
contraseña de Gameforge; la contraseña no se muestra. El asistente guarda el
acceso en el Administrador de credenciales de tu usuario de Windows y activa
`session.auto_login`. Reinicia el bot y abre de nuevo `Abrir_panel.bat`.
En Configuración general puedes activar/desactivar el uso de ese acceso y
ajustar la espera entre intentos. La contraseña nunca se pide desde el panel.

El registro de credencial está ligado al jugador y universo configurados.
No se guarda en YAML, variables de entorno, GitHub o registros, y no se usan
contraseñas del navegador ni credenciales de otras aplicaciones. Para retirar
la credencial guardada y desactivar esta función, ejecuta `Desactivar_acceso.bat`.
La cuenta de Windows que ejecute el bot debe ser la misma que configuró el
acceso. En otros sistemas se conserva el acceso manual, sin almacenamiento
alternativo en texto plano.

Opciones no secretas (opcionales y compatibles con configuraciones anteriores):

```yaml
session:
  auto_login: false
  login_retry_seconds: 300  # mínimo 60; máximo tres envíos por proceso
```

El fragmento añade opciones a la sección existente, no sustituye sus otros
campos. Si caduca el Lobby, solo se rellena un formulario de acceso reconocido
en los orígenes HTTPS oficiales admitidos. Se verifica de nuevo el destino antes
de introducir la contraseña y de enviar el formulario. Formularios ambiguos,
registro, acceso social y orígenes desconocidos requieren acceso manual. Un
CAPTCHA, un código de verificación o un rechazo detienen los nuevos envíos de
credenciales; puedes completar la comprobación personalmente. La selección de
universo y la verificación de identidad se mantienen tras iniciar sesión.

La API nativa utilizada es `CredWriteW`/`CredReadW`/`CredDeleteW`, con credenciales
genéricas persistentes en este PC para el usuario actual. Referencias:
[CredWriteW](https://learn.microsoft.com/windows/win32/api/wincred/nf-wincred-credwritew),
[CREDENTIALW](https://learn.microsoft.com/windows/win32/api/wincred/ns-wincred-credentialw).
Las pruebas usan accesos sintéticos y no leen credenciales de cuentas reales.
La prueba nativa opcional solo escribe y retira una clave aleatoria del espacio
`OrbitOGame/Lobby/v1/test-*` (`OGAME_CREDENTIAL_TESTS=1`). El envío de credenciales
al formulario real aún requiere validación con el acceso configurado localmente.
En esta entrega, la prueba nativa no pudo guardar la credencial sintética desde
la sesión de desarrollo: Windows devolvió error 5 (acceso denegado). Debe
comprobarse desde la sesión normal del usuario con `Configurar_acceso.bat`;
no se recurre a almacenamiento alternativo si Windows deniega el acceso.
