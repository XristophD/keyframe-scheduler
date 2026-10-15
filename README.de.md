# Keyframe Scheduler

Home Assistant Integration für zeitbasierte Lichtsteuerung mit Keyframes: Helligkeit und Farbtemperatur über den Tag, nach Uhrzeit oder nach dem Sonnenstand.

Funktioniert mit **allen** HA-Light-Entities — DALI, Casambi, Zigbee, Philips Hue, Z-Wave, WLED, PICOlightnode und Standard-Leuchten.

> Also available in [English](README.md) | También disponible en [Español](README.es.md)

---

## Funktionsweise

Du definierst Keyframes — jeweils mit Auslöser, Helligkeit und Farbtemperatur. Die Integration interpoliert zwischen ihnen und **steuert die zugeordneten Leuchten selbst**. Es ist keine Automation und kein Blueprint nötig.

- **Auslöser:** eine feste Uhrzeit oder ein Sonnenereignis am Standort (Dämmerung, Sonnenaufgang, Sonnenmittag, goldene Stunde, Sonnenuntergang, Sonnenmitternacht) mit Versatz in Minuten und optionalen Grenzen *frühestens / spätestens*.
- **Gruppen:** Keyframes mit derselben Rolle (z. B. „Sonnenuntergang“ und „20:00“) lassen sich gruppieren; pro Tag gilt dann nur der früheste, der späteste oder alle.
- **Gültig an:** ein Keyframe lässt sich auf Wochentage und/oder einen Zeitraum im Jahr beschränken (z. B. Mo–Fr, 01.11.–28.02.), wie *valid on* im PICO. Ein Tag ganz ohne Keyframes hält den letzten Wert; über Mitternacht zählt der tatsächliche Vortag.
- **Eine Instanz = ein Zeitplan** für beliebig viele Leuchten. Eine weitere Instanz braucht es nur für einen anderen Zeitplan.

---

## Installation

### Via HACS (empfohlen)

1. HACS → Integrations → `+` → **Keyframe Scheduler** suchen
2. Installieren → Home Assistant neu starten

---

## Setup

### Schritt 1 — Instanz erstellen

1. Einstellungen → Geräte & Dienste → Integration hinzufügen → **Keyframe Scheduler**
2. Namen vergeben (z. B. `Meetingraum`)

### Schritt 2 — Zeitplan gestalten

In der Webapp (Sidebar-Eintrag **Keyframe Scheduler**) den Zeitplan gestalten und mit **Als Datei speichern** exportieren. Die Webapp zeigt eine Jahres- und eine Tagesansicht mit Sonnenzeiten, markiert Keyframes, die im Jahresverlauf ihre Reihenfolge tauschen, und schlägt passende Grenzen vor.

### Schritt 3 — Zeitplan und Leuchten zuordnen

Einstellungen → Geräte & Dienste → Keyframe Scheduler → **Konfigurieren**:

| Feld | Beschreibung |
|------|--------------|
| **Zeitplan-JSON** | Inhalt der exportierten Datei einfügen (leer lassen = unverändert) |
| **Leuchten** | Die Leuchten dieser Instanz — auch Leuchtengruppen (werden in ihre Mitglieder aufgelöst) |
| **Nach manueller Änderung wieder folgen** | Nur durch Aus/Ein oder den Folgen-Schalter · nach X Minuten · beim nächsten Keyframe |
| **Auch Änderungen am Gerät erkennen** | Erkennt z. B. einen Wand-Dimmer am Bus (siehe unten) |
| **Zeiten der Leuchtentypen anpassen** | Öffnet einen weiteren Schritt für die Typ-Zeiten |

Im nächsten Schritt bekommt jede Leuchte ihren **Typ**:

| Typ | Max. Übergang | Min. Abstand zwischen Befehlen |
|-----|---------------|--------------------------------|
| DALI | 90 s | 30 s |
| DALI-2 Extended Fade | 15 min | 30 s |
| Casambi / Bluetooth Mesh | 10 min | 30 s |
| Zigbee | 10 min | 15 s |
| Philips Hue | 10 min | 10 s |
| Z-Wave | 5 min | 30 s |
| Generic / WLAN | 5 min | 15 s |

Die Abstände sind vorsichtig gewählt und pro Instanz anpassbar. So lassen sich Leuchten verschiedener Bussysteme in **einem** Zeitplan mischen, z. B. DALI und Zigbee im selben Meetingraum.

Beim Standort für Sonnen-Keyframes gilt: der Standort aus dem Zeitplan, sonst der in Home Assistant eingestellte.

---

## Verhalten der Leuchten

- **Nur eingeschaltete Leuchten werden nachgeführt.** Die Integration schaltet nie selbst ein. Ausschalten am Wandschalter heißt „aus“.
- **Beim Einschalten** (App, Wandschalter, Präsenzmelder …) übernimmt die Leuchte sofort den aktuellen Wert und folgt ab dann dem Zeitplan.
- **Jede Leuchte läuft im Takt ihres eigenen Typs**, auch wenn mehrere Typen in einer Instanz stecken: Sie folgt der Kurve des Zeitplans in Fades, so lang ihr Typ erlaubt, und jeder endet auf der Kurve. Eine DALI-Leuchte bekommt etwa alle 90 s einen Befehl, eine Zigbee-Leuchte alle 10 min, eine DALI-2-Leuchte alle 15 min – für dieselbe Kurve. Lange Transitionen und Interpolationen sind deshalb nie zu früh am Ziel.
- **Instant-Keyframes** schalten zu ihrer Zeit mit 1 s Überblendung.

### Manuelle Änderungen

Ändert jemand anderes die Leuchte, **pausiert** sie: Ihr Folgen-Schalter geht aus, die Integration sendet ihr nichts mehr.

Als manuelle Änderung gilt:
- ein Befehl von einem Benutzer (Dashboard, App)
- ein Befehl einer Szene, eines Skripts oder einer anderen Automation
- optional eine Änderung, die die Leuchte selbst meldet (z. B. Wand-Dimmer am Bus): erkannt, wenn der gemeldete Wert nach Ende des Übergangs deutlich vom gesendeten abweicht (> 5 % Helligkeit oder > 150 K)

Nicht als manuelle Änderung gilt: die eigenen Befehle der Integration und PICOlightnode-interne Updates (`picolightnode_restore`).

**Fortsetzen:** Aus- und wieder Einschalten der Leuchte setzt sie **immer** zurück. Zusätzlich je nach Einstellung nach X Minuten oder beim nächsten Keyframe.

### Folgen-Schalter

Pro Leuchte gibt es einen Schalter:
```
switch.keyframe_<instanz>_<leuchte>_follow
```

| Zustand | Bedeutung |
|---------|-----------|
| AN | Die Leuchte folgt dem Zeitplan |
| AUS, `pause_reason: manual` | Pausiert durch eine manuelle Änderung — wird automatisch fortgesetzt (s. o.) |
| AUS, `pause_reason: user` | Bewusst ausgeschaltet — bleibt aus, bis der Schalter wieder eingeschaltet wird |

Beim Wiedereinschalten des Schalters blendet die Leuchte sofort auf den aktuellen Wert über.

---

## Sensoren

Pro Instanz:

| Sensor | Beschreibung |
|--------|-------------|
| `sensor.<name>_target_kelvin` | Aktueller Farbtemperatur-Zielwert in Kelvin |
| `sensor.<name>_target_brightness` | Aktueller Helligkeits-Zielwert (0–100 %) |
| `sensor.<name>_target_mired` | Aktuelle Farbtemperatur in Mired |
| `sensor.<name>_next_change` | Zeitpunkt der nächsten geplanten Wertänderung |

Attribute: `transition_seconds` (Überblendzeit bis zum nächsten Update) und `keyframes_today` (wann die Keyframes heute auslösen, z. B. `["07:00", "22:03 (sunset +30 min)"]`).

---

## Dienste

| Dienst | Beschreibung |
|--------|--------------|
| `keyframe_scheduler.turn_on` | Leuchten **mit den aktuellen Werten des Zeitplans** einschalten – sie gehen gleich richtig an statt mit ihrem gespeicherten Zustand und einer sichtbaren Korrektur eine Sekunde später. Jede Leuchte bekommt, was sie darstellen kann; eine pausierte Leuchte folgt wieder |
| `keyframe_scheduler.toggle` | Wie `light.toggle`, schaltet aber mit den Werten des Zeitplans ein – der Ersatz für Wandtaster |
| `keyframe_scheduler.apply` | Aktuelle Werte sofort an die Leuchten senden (optional nur eine Instanz / bestimmte Leuchten) |
| `keyframe_scheduler.set_manual_control` | Leuchten pausieren (`manual: true`) oder wieder folgen lassen (`manual: false`) |
| `keyframe_scheduler.set_schedule` | Zeitplan als JSON setzen |
| `keyframe_scheduler.upload_from_file` | Zeitplan aus einer Datei unter `/config/` laden |

Ein neuer Zeitplan über `set_schedule` oder `upload_from_file` gilt sofort, ohne die Integration neu zu laden.

**Wandtaster und Präsenz-Automationen:** `keyframe_scheduler.toggle` / `turn_on` statt `light.toggle` / `light.turn_on` verwenden. Eine Gruppe, deren Mitglieder dieselben Werte bekommen, wird in einem Aufruf geschaltet (ein Hue-Raum oder eine Zigbee-Gruppe geht gleichzeitig an). Leuchten, die außerhalb von Home Assistant eingeschaltet werden (Hue-App, Hue-Dimmschalter, direkt am Bus gebundene Schalter), gehen weiterhin mit ihrem gespeicherten Zustand an und werden eine Sekunde später korrigiert.

```yaml
# Wandtaster: Küche mit den Werten des Zeitplans umschalten
actions:
  - action: keyframe_scheduler.toggle
    target:
      entity_id: light.kuche
```

---

## Umstieg vom Blueprint

Bis Version 3.x hat eine Blueprint-Automation pro Leuchte die Werte angewendet. Das Blueprint wurde entfernt:

1. Die bisherigen Blueprint-Automationen **löschen** — sonst steuern zwei Stellen dieselbe Leuchte.
2. Die bisherigen „Follow Lights“ werden automatisch als Leuchten übernommen (mit dem Typ zum bisherigen Hardware-Limit). Unter **Konfigurieren** prüfen und die Typen pro Leuchte setzen.

---

## Webapp

Sobald die erste Instanz angelegt ist (Schritt 1), erscheint **Keyframe Scheduler** als Sidebar-Eintrag in Home Assistant – den Ordner zu kopieren genügt nicht. Fehlt der Eintrag danach noch, den Browser neu laden (Strg+F5). Direkte URL: `http://<dein-ha-host>/keyframe_scheduler/index.html`

Verfügbare Sprachen: DE / EN / ES

### Export für PICO lightnode

**PICO DailyScheduler** (Export & Import) erzeugt das `DAILYSCHEDULER`-Behavior für das `setup.json` eines PICO lightnode: in die `behaviors` eines Targets mit Space `TC` einfügen. Targets, Adjust-/Override-Behaviors und Destinations bleiben im Setup; Koordinaten und Zeitzone kommen aus der PICO-Konfiguration (`pico.latitude` / `pico.longitude`).

Sonnen-Trigger, Versatz, Früh-/Spätgrenzen und Gruppen (`EARLIEST`/`LATEST`) werden 1:1 übersetzt. Ausgang **DALI** (Standard) hält jeden Fade bei höchstens 15 min: längere Fades werden in ganze Minuten zerlegt, die der Kurve des Keyframes folgen (die Datei wird dadurch größer). Ausgang **DMX / andere** schreibt einen Eintrag pro Keyframe. Der PICO blendet nur linear und mit fester Dauer – Sinus-Kurven werden linear, und eine an die Sonne gebundene Interpolation nutzt die kürzeste Rampe des Jahres (das Ziel wird früher erreicht und gehalten). Die Exportansicht listet jede Abweichung zur Simulation.

---

## Voraussetzungen

| Komponente | Mindestversion |
|------------|---------------|
| Home Assistant | 2024.7.0 |
| PICOlightnode *(optional)* | 2.0.18 |

---

## Links

- [Fehler & Feature-Anfragen](https://github.com/mjmijh/keyframe-scheduler/issues)
- [PICOlightnode Integration](https://github.com/mjmijh/picolightnode-ha)
- [CCT Astronomy Integration](https://github.com/mjmijh/cct-astronomy)
