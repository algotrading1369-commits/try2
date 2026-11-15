# Energía, almacenes, flotas y recuperación

## Prioridad de almacenes

En Construcción está activada por defecto la ampliación automática al 90% de
llenado. Se amplía primero el almacén del recurso más saturado y después se
retoman las minas, tanto en ROI como en prioridad. Se respetan las reservas,
los campos disponibles, el nivel máximo de almacén y las obras ya en curso.
El ROI excluye una mina cuyo recurso esté lleno hasta resolver su capacidad;
no descuenta con precisión todas las pérdidas futuras de producción.

Las capacidades se leen de atributos explícitos del DOM o de tooltips con la
etiqueta de capacidad de almacenamiento. Si la lectura es desconocida, se
informa y no se inventa un límite. Tampoco se interpreta un planeta sin campos
como un almacén lleno: otro almacén también necesita un campo. No se construye
automáticamente un Terraformer ni se demuelen edificios.

## Satélites solares

Construcción permite elegir Planta solar o Satélites solares como fuente
automática. La opción Resolver automáticamente la falta de energía controla
ambas. Los satélites se fabrican en lotes limitados por la configuración del
hangar y por el máximo de satélites por planeta; no ocupan campos.

El modelo base usa `floor((temperatura máxima + 140) / 6)`. Puedes sustituirlo
por la energía por satélite observada en tu cuenta, incluyendo sus bonos. Sin
temperatura ni valor explícito no se inventa producción. Tras fabricar se
confirma el inventario y el fin de cola, y se relee la energía antes de aprobar
la mina. El ROI incluye el coste de los satélites requeridos por esa mejora.
El modelo base se contrastó con el hilo de fórmulas del foro de OGame:
https://board.en.ogame.gameforge.com/index.php?thread/548906-formula-thread-v3/

Los satélites automáticos usan el permiso real de economía; la cola manual de
naves conserva el permiso independiente de Hangar. Pueden perderse en combate.

## Transportes, reciclaje y defensa

Cada módulo tiene un interruptor independiente para envíos reales. Se pueden
mantener las demás misiones en simulación sin cambiar `automation.dry_run`.
Los transportes verifican el destino propio, el inventario, las reservas y la
ocupación mínima. El reciclaje vuelve a comprobar el campo de escombros antes
del envío y exige verificar el sistema observado.

La lectura de movimientos admite tarjetas `fleetDetails` con identificador,
dirección, misión, coordenadas y tablas explícitas de naves/recursos. Una lista
vacía requiere un mensaje explícito. Filas ambiguas, cargas incompletas o un
formulario que no coincide impiden enviar; no se confirma a partir de un clic.
Estos contratos DOM requieren todavía contraste con la interfaz actual de la
cuenta. Las pruebas sintéticas no acreditan compatibilidad completa.

Defensa ya no confunde el combustible máximo configurado con un gasto
obligatorio para proponer una evacuación. La cotización real sigue siendo
obligatoria antes del envío. El estado defensivo desconocido continúa visible
y puede pausar las rutinas según la configuración.

## FleetSave independiente

La pestaña FleetSave permite un despliegue a otro cuerpo propio, inmediatamente
o a una fecha/hora local. Se verifican todas las naves móviles, la duración
mínima y la carga con el combustible real. Puede utilizar el slot reservado a
las rutinas ordinarias. No garantiza por sí solo que el destino sea seguro.

Cada plan tiene un ID duradero. Una salida confirmada no se repite al reiniciar;
para otra salida cambia el ID. Una orden incierta se conserva y bloquea otra
salida hasta verificarla. No se cancela automáticamente una flota. Las alertas
reactivas del defensor siguen siendo independientes de esta programación.

## Estabilidad

La recuperación aislada está activada por defecto y puede cambiarse en General.
Un módulo no crítico que acumula errores recuperables de lectura o sesión
entra en una pausa y después vuelve a intentarlo. Los demás trabajos siguen
ejecutándose. Los errores críticos y de programación no se ocultan como éxito.

La prueba recorre 24 horas virtuales del scheduler con fallos transitorios,
pausas de recuperación y caducidad de sesión, comprobando que las revisiones
defensivas continúan. También se prueban confirmaciones y persistencia al
reiniciar. No es una prueba de 24 horas de navegador real, ni sustituye la
validación de transportes, reciclaje y evacuaciones en el universo.
