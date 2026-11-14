# Hangar y expediciones

## Hangar

En la pestaña Hangar, habilita la cola y añade filas con nave, planeta y cantidad
total que quieres fabricar. El máximo por lote limita cada envío al hangar; si
los recursos solo alcanzan para una parte, fabrica esa parte y espera para el
resto, respetando las reservas de Construcción. La preparación antigua de sonda
debe estar desactivada o completada para usar la cola normal.

La cantidad significa unidades que se fabricarán desde esta orden, no stock
que mantener. Por ejemplo, una orden de 8 cargueras con lote máximo 3 avanza
3 + 3 + 2. Reiniciar el bot o enviar las cargueras de misión no repite esa orden.
Puedes aumentar la cantidad de la misma fila para ampliar el objetivo. Una
fila nueva tiene un identificador nuevo y representa otra fabricación.

El historial se confirma al observar el inventario objetivo. Mientras una
fabricación está pendiente de confirmar, las rutinas de flota no usan ese origen.
Mover manualmente sus naves antes de esa confirmación puede impedir verificarla;
no se borra ni reenvía una orden incierta. Edificios, investigación y hangar
conservan sus colas separadas. La cola no construye requisitos automáticamente:
si la tarjeta del juego está bloqueada, se informa y se espera.

`shipyard.live` controla la fabricación real de esta cola. Empieza con una
cantidad pequeña y comprueba el registro de órdenes. No es necesario cambiar
`automation.dry_run` ni detener el bot para guardar las instrucciones.

```yaml
shipyard:
  enabled: true
  live: false
  max_batch: 3
  queue:
    - id: cargueras-prueba
      body_id: 101  # Sustituir por un planeta leído de tu cuenta.
      item: small_cargo
      quantity: 8
```

## Expediciones

Activa Gestor de flotas y Planificar expediciones. Selecciona los orígenes,
permanencia y máximo simultáneo. En Manual introduce las naves por envío; no
necesitas top 1 ni capacidades teóricas para proponer esa composición. La
cotización final del juego debe confirmar combustible, carga y duración antes
del clic de envío. El combustible máximo es un límite, no un gasto estimado.

En Automática, configura velocidad económica, clase y capacidades efectivas
de tus naves. El modelo calcula la capacidad a partir del top 1 y bonificaciones;
no sustituye una comprobación del universo ni garantiza el resultado aleatorio.
Los costes de naves se contrastaron con la tabla de Gameforge:
https://gameforge.com/en-GB/games/browser-game-with-spaceship-building.html
El modelo histórico de expediciones se documenta en la guía del foro:
https://board.en.ogame.gameforge.com/index.php?thread/810634-guide-10-expedition-guide/

`fleet.expeditions.live` habilita exclusivamente expediciones reales mientras
`automation.dry_run` mantiene las otras misiones en simulación. El envío se
registra antes del clic y se confirma contra un movimiento nuevo. Un envío
incierto bloquea el siguiente; no se repite a ciegas. Si cambia el formulario,
se informa `fleet_form_unverified` sin detener la construcción y la defensa.

Tras confirmar un envío se releen slots e inventarios antes de llenar otro.
Cuando todos los slots configurados están ocupados, el gestor espera su intervalo
de flotas. En una revisión posterior, un regreso observado libera un slot para
otro envío. No hay una orden adicional para volver: la expedición regresa por
la mecánica del juego. No se cancela ni acelera automáticamente el regreso.

## Validación

Las pruebas cubren lotes, reservas, persistencia de cantidades confirmadas,
simulación, selector de cantidad, rechazo de coste insuficiente, composición
manual, slots y reposición tras un regreso simulado. La interfaz del panel se
comprueba por separado. El formulario de flota actual y un envío/regreso reales
siguen pendientes de validación; una prueba con HTML sintético no los acredita.
