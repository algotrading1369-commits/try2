# Preparar una sonda

El modo de preparación se configura con tres campos opcionales:

```yaml
automation:
  dry_run: true
  probe_target: 1
  probe_body_id: 101
  probe_bootstrap_live: false
```

El identificador 101 es un ejemplo: debe ser un planeta de la cuenta.
Los demás campos habituales de `automation` se conservan. Por defecto la
preparación está desactivada (`probe_target: 0`).

La secuencia comprueba inventario, recursos, colas y requisitos. Puede subir
Robots a 2, Laboratorio a 3, Hangar a 3, Energía a 1, Combustión a 3 y Espionaje
a 2, solo si faltan, y después fabricar una sonda. Espera la confirmación de
cada mejora antes de la siguiente. Mientras este modo está activo, el Brain
no ejecuta las prioridades económicas normales. Se respetan las reservas y
el bloqueo por ataques. No se usan botones de materia oscura ni aceleración.

`probe_bootstrap_live: true` es una excepción explícita y limitada a la
simulación: habilita solo esas mejoras y la fabricación de una sonda en el
planeta configurado. `dry_run: true` sigue impidiendo los envíos de flota.
La consola muestra el alcance real al arrancar. Con `probe_bootstrap_live:
false`, toda la preparación permanece simulada.

Las órdenes se guardan antes del clic. Un envío incierto no se repite. La
sonda se confirma al observarla en el inventario del hangar; en cuanto queda
confirmada, el registro bloquea nuevas fabricaciones del modo de preparación,
incluso tras reiniciar o enviar la sonda a una misión. No borres el registro
para repetir la prueba.

Usa `Abrir_bot.bat`. El informe de planificación es `runtime/plan.json`. Esta
preparación no acredita que el defensor pueda enviar una flota en vivo.
