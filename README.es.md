# Keyframe Scheduler

Integración de Home Assistant para el control de iluminación basado en keyframes: brillo y temperatura de color a lo largo del día, por hora del reloj o según la posición del sol.

Funciona con **todas** las entidades de luz de HA — DALI, Casambi, Zigbee, Philips Hue, Z-Wave, WLED, PICOlightnode y luces estándar.

> Also available in [English](README.md) | Auch verfügbar auf [Deutsch](README.de.md)

---

## Cómo funciona

Defines keyframes — cada uno con un disparador, brillo y temperatura de color. La integración interpola entre ellos y **controla ella misma las luces asignadas**. No hace falta ninguna automatización ni blueprint.

- **Disparadores:** una hora fija o un evento solar en la ubicación (crepúsculos, salida del sol, mediodía solar, hora dorada, puesta del sol, medianoche solar) con un desfase en minutos y límites opcionales *no antes de / no después de*.
- **Grupos:** los keyframes con la misma función (p. ej. «puesta del sol» y «20:00») se pueden agrupar; cada día solo se aplica el primero, el último o todos.
- **Válido en:** un keyframe se puede limitar a días de la semana y/o a un periodo del año (p. ej. Lu–Vi, 01.11.–28.02.), como *valid on* en el PICO. Un día sin keyframes mantiene el último valor; a medianoche cuenta el día anterior real.
- **Una instancia = un horario** para cualquier número de luces. Otra instancia solo hace falta para un horario distinto.

---

## Instalación

### Via HACS (recomendado)

1. HACS → Integraciones → `+` → buscar **Keyframe Scheduler**
2. Instalar → reiniciar Home Assistant

---

## Configuración

### Paso 1 — Crear una instancia

1. Configuración → Dispositivos y servicios → Añadir integración → **Keyframe Scheduler**
2. Asignar un nombre (p. ej. `Sala de reuniones`)

### Paso 2 — Diseñar el horario

Diseña el horario en la webapp (entrada de la barra lateral **Keyframe Scheduler**) y expórtalo con **Guardar como archivo**. La webapp muestra una vista anual y una diaria con las horas solares, marca los keyframes que cambian de orden a lo largo del año y sugiere límites adecuados.

### Paso 3 — Asignar horario y luces

Configuración → Dispositivos y servicios → Keyframe Scheduler → **Configurar**:

| Campo | Descripción |
|-------|-------------|
| **JSON del horario** | Pegar el contenido del archivo exportado (vacío = sin cambios) |
| **Luces** | Las luces de esta instancia — también grupos de luces (se expanden en sus miembros) |
| **Volver a seguir tras un cambio manual** | Solo apagando/encendiendo o con el interruptor de seguimiento · tras N minutos · en el siguiente keyframe |
| **Detectar también cambios en el dispositivo** | Detecta p. ej. un regulador de pared en el bus (ver abajo) |
| **Ajustar los tiempos de los tipos de luz** | Abre un paso adicional para los tiempos de cada tipo |

En el paso siguiente cada luz recibe su **tipo**:

| Tipo | Transición máx. | Intervalo mín. entre comandos |
|------|-----------------|-------------------------------|
| DALI | 90 s | 30 s |
| DALI-2 Extended Fade | 15 min | 30 s |
| Casambi / Bluetooth Mesh | 10 min | 30 s |
| Zigbee | 10 min | 15 s |
| Philips Hue | 10 min | 10 s |
| Z-Wave | 5 min | 30 s |
| Genérico / WiFi | 5 min | 15 s |

Los intervalos son valores prudentes y se pueden ajustar por instancia. Así, luces de distintos buses pueden compartir **un** horario, p. ej. DALI y Zigbee en la misma sala de reuniones.

Ubicación para los keyframes solares: la del horario; si no la tiene, la configurada en Home Assistant.

---

## Comportamiento de las luces

- **Solo se ajustan las luces encendidas.** La integración nunca enciende una luz. Apagar en el interruptor de pared significa «apagada».
- **Al encenderla** (app, interruptor de pared, detector de presencia …) la luz toma de inmediato los valores actuales y sigue el horario desde ese momento.
- **Cada luz va al ritmo de su propio tipo**, también cuando varios tipos comparten una instancia: sigue la curva del horario en fundidos tan largos como permite su tipo, y cada uno termina sobre la curva. Una luz DALI recibe un comando cada 90 s aproximadamente, una Zigbee cada 10 min, una DALI-2 cada 15 min – para la misma curva. Por eso las transiciones e interpolaciones largas nunca terminan antes de tiempo.
- **Los keyframes instantáneos** conmutan a su hora con un fundido de 1 s.

### Cambios manuales

Si otra persona o sistema cambia la luz, esta se **pausa**: su interruptor de seguimiento se apaga y la integración deja de enviarle comandos.

Se considera cambio manual:
- un comando de un usuario (panel, app)
- un comando de una escena, un script u otra automatización
- opcionalmente un cambio que la propia luz notifica (p. ej. regulador de pared en el bus): se detecta cuando el valor notificado difiere claramente del enviado una vez terminado el fundido (> 5 % de brillo o > 150 K)

No se considera cambio manual: los comandos propios de la integración ni las actualizaciones internas de PICOlightnode (`picolightnode_restore`).

**Reanudar:** apagar y volver a encender la luz la reanuda **siempre**. Según el ajuste, también tras N minutos o en el siguiente keyframe.

### Interruptor de seguimiento

Cada luz tiene un interruptor:
```
switch.keyframe_<instancia>_<luz>_follow
```

| Estado | Significado |
|--------|-------------|
| ON | La luz sigue el horario |
| OFF, `pause_reason: manual` | Pausada por un cambio manual — se reanuda automáticamente (ver arriba) |
| OFF, `pause_reason: user` | Apagado a propósito — permanece apagado hasta volver a encender el interruptor |

Al volver a encender el interruptor, la luz pasa de inmediato a los valores actuales con un fundido suave.

---

## Sensores

Por instancia:

| Sensor | Descripción |
|--------|-------------|
| `sensor.<nombre>_target_kelvin` | Temperatura de color objetivo actual en Kelvin |
| `sensor.<nombre>_target_brightness` | Brillo objetivo actual (0–100 %) |
| `sensor.<nombre>_target_mired` | Temperatura de color actual en mired |
| `sensor.<nombre>_next_change` | Momento del próximo cambio de valor previsto |

Atributos: `transition_seconds` (tiempo de fundido hasta la siguiente actualización) y `keyframes_today` (cuándo se activan hoy los keyframes, p. ej. `["07:00", "22:03 (sunset +30 min)"]`).

---

## Servicios

| Servicio | Descripción |
|----------|-------------|
| `keyframe_scheduler.turn_on` | Encender luces **con los valores actuales del horario**: se encienden ya correctas en lugar de con su estado guardado y una corrección visible un segundo después. Cada luz recibe lo que puede mostrar; una luz en pausa vuelve a seguir el horario |
| `keyframe_scheduler.toggle` | Como `light.toggle`, pero enciende con los valores del horario: el sustituto para pulsadores de pared |
| `keyframe_scheduler.apply` | Enviar ya los valores actuales a las luces (opcionalmente una instancia / luces concretas) |
| `keyframe_scheduler.set_manual_control` | Pausar luces (`manual: true`) o volver a seguir el horario (`manual: false`) |
| `keyframe_scheduler.set_schedule` | Definir el horario como JSON |
| `keyframe_scheduler.upload_from_file` | Cargar el horario desde un archivo en `/config/` |

Un horario nuevo mediante `set_schedule` o `upload_from_file` se aplica de inmediato, sin recargar la integración.

**Pulsadores de pared y automatizaciones de presencia:** usa `keyframe_scheduler.toggle` / `turn_on` en lugar de `light.toggle` / `light.turn_on`. Un grupo cuyos miembros reciben los mismos valores se conmuta en una sola llamada (una habitación Hue o un grupo Zigbee se enciende a la vez). Las luces encendidas fuera de Home Assistant (app Hue, regulador Hue, interruptores vinculados directamente en el bus) siguen encendiéndose con su estado guardado y se corrigen un segundo después.

```yaml
# Pulsador: alternar la cocina con los valores del horario
actions:
  - action: keyframe_scheduler.toggle
    target:
      entity_id: light.kuche
```

---

## Migración desde el blueprint

Hasta la versión 3.x una automatización de blueprint por luz aplicaba los valores. El blueprint se ha eliminado:

1. **Eliminar** las automatizaciones de blueprint existentes — si no, dos sitios controlan la misma luz.
2. Las antiguas «Follow Lights» se adoptan automáticamente como luces (con el tipo correspondiente al antiguo límite de hardware). Revísalas en **Configurar** y asigna el tipo de cada luz.

---

## Webapp

En cuanto se crea la primera instancia (paso 1), **Keyframe Scheduler** aparece como entrada en la barra lateral de Home Assistant; copiar la carpeta no basta. Si la entrada sigue sin aparecer, recarga el navegador (Ctrl+F5). URL directa: `http://<tu-host-ha>/keyframe_scheduler/index.html`

Idiomas disponibles: DE / EN / ES

Abierta desde la barra lateral, la lista de ejemplos ofrece también los **horarios de tus instancias** (grupo *Home Assistant*): elige uno para cargarlo y editarlo, y guárdalo de nuevo con *Configurar* o `set_schedule`.

### Exportación para PICO lightnode

**PICO DailyScheduler** (Exportar e importar) genera el behavior `DAILYSCHEDULER` para el `setup.json` de un PICO lightnode: pégalo en los `behaviors` de un target con space `TC`. Targets, behaviors de ajuste/override y destinations permanecen en el setup; coordenadas y zona horaria vienen de la configuración del PICO (`pico.latitude` / `pico.longitude`).

Disparadores solares, desplazamiento, límites y grupos (`EARLIEST`/`LATEST`) se traducen 1:1. La salida **DALI** (por defecto) mantiene cada fundido en 15 min como máximo: los fundidos más largos se dividen en minutos enteros que siguen la curva del keyframe (el archivo crece). La salida **DMX / otros** escribe una entrada por keyframe. El PICO solo funde linealmente y con duración fija – las curvas sinusoidales se vuelven lineales y una interpolación ligada al sol usa la rampa más corta del año (el objetivo se alcanza antes y se mantiene). La vista de exportación lista cada diferencia con la simulación.

---

## Requisitos

| Componente | Versión mínima |
|------------|---------------|
| Home Assistant | 2024.7.0 |
| PICOlightnode *(opcional)* | 2.0.18 |

---

## Enlaces

- [Problemas y solicitudes de funciones](https://github.com/mjmijh/keyframe-scheduler/issues)
- [Integración PICOlightnode](https://github.com/mjmijh/picolightnode-ha)
- [Integración CCT Astronomy](https://github.com/mjmijh/cct-astronomy)
