"""
SNIPER Autius: consulta el calendario cada segundo y, en cuanto salen huecos,
reserva 2 clases seguidas con el mismo profesor en 2 días de la semana objetivo.

Reglas (editar abajo si cambian):
  - Semana: 19-23 oct 2026. Prioridad de días: martes, miércoles, luego lunes, viernes.
  - Lun/Mar/Mié: empieza a partir de las 18:45. Viernes: a partir de las 16:00. Jueves y finde: no.
  - En cada día: 2 clases de 45 min seguidas (una termina cuando empieza la otra) con el mismo profesor.
  - Objetivo: 2 días con su pareja de clases = 4 clases.

Modos (variable MODE):
  probe -> NO reserva nada. Hace una reserva falsa a un id inexistente para ver qué pide la API.
  dry   -> NO reserva nada. Muestra qué reservaría ahora mismo.
  live  -> Vigila cada segundo durante RUN_MINUTES y reserva.
  test  -> RESERVA DE VERDAD 1 sola clase (la primera libre) del día TEST_DATE, para comprobar que reservar funciona.
"""
import os, time, smtplib, datetime as dt
from concurrent.futures import ThreadPoolExecutor
from email.message import EmailMessage
from zoneinfo import ZoneInfo
import requests

# ---------------- CONFIGURACIÓN ----------------
TZ = ZoneInfo("Europe/Madrid")
WEEK_FROM, WEEK_TO = "2026-10-19", "2026-10-23"
# Día de la semana (0=lunes) -> hora mínima de inicio. Orden = prioridad.
DAY_RULES = [(1, "18:45"), (2, "18:45"), (0, "18:45"), (4, "16:00")]
DAYS_WANTED = 2
LESSON_TYPE = "class_45"          # clase individual de 45 min
POLL_SECONDS = 1.0
RUN_MINUTES = float(__import__("os").environ.get("RUN_MINUTES") or 15)   # cuánto rato vigila desde que lo lanzas
# ------------------------------------------------

BASE = "https://api.autius.com/api"
MODE = os.environ.get("MODE", "live").strip().lower()
COOKIE = os.environ["AUTIUS_COOKIE"]
GMAIL_USER = os.environ.get("GMAIL_USER")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")
EMAIL_TO = os.environ.get("EMAIL_TO") or GMAIL_USER

S = requests.Session()
S.headers.update({
    "Cookie": COOKIE,
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Origin": "https://app.autius.com",
    "Referer": "https://app.autius.com/",
})


def log(*a):
    print(dt.datetime.now(TZ).strftime("%H:%M:%S.%f")[:-3], *a, flush=True)


def send_email(subject, body):
    if not (GMAIL_USER and GMAIL_APP_PASSWORD):
        return
    try:
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = subject, GMAIL_USER, EMAIL_TO
        msg.set_content(body)
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            smtp.send_message(msg)
    except Exception as e:
        log("Error enviando email:", e)


def hhmm(t):
    return (t or "")[:5]


def teacher(s):
    i = s.get("instructor") or {}
    return f"{i.get('firstName','')} {i.get('lastName','')}".strip()


def label(s):
    return f"{s['date']} {hhmm(s['startTime'])}-{hhmm(s['endTime'])} · {teacher(s)}"


def fetch_week():
    r = S.get(f"{BASE}/v1/student/lessons/calendar",
              params={"from": WEEK_FROM, "to": WEEK_TO}, timeout=5)
    if r.status_code in (401, 403):
        send_email("SNIPER Autius: sesión caducada", "Actualiza AUTIUS_COOKIE en GitHub.")
        raise SystemExit("Sesión caducada")
    r.raise_for_status()
    return r.json().get("slots", [])


EXIT_POINTS = {}   # "NOMBRE APELLIDO" -> exitPointId (se carga del booking-catalog)


def load_exit_points():
    try:
        r = S.get(f"{BASE}/v1/student/lessons/booking-catalog", timeout=10)
        for i in r.json().get("instructors", []):
            EXIT_POINTS[i.get("displayName", "").strip().upper()] = i.get("exitPointId")
    except Exception as e:
        log("No pude cargar puntos de salida:", e)


def reserve(slot):
    """Devuelve (ok, status, texto). Si la API pide datos (400/422), reintenta con el punto de salida."""
    url = f"{BASE}/v1/student/lessons/reserve/{slot['id']}"
    try:
        r = S.post(url, json={}, timeout=8)
        if r.status_code in (400, 422):
            ep = EXIT_POINTS.get(teacher(slot).upper()) or next(iter(EXIT_POINTS.values()), None)
            log(f"Reserva pidió datos [{r.status_code}] {r.text[:200]} -> reintento con exitPointId")
            r = S.post(url, json={"exitPointId": ep}, timeout=8)
        return r.ok, r.status_code, r.text[:300]
    except requests.RequestException as e:
        return False, 0, str(e)


# ---------------- lógica de selección ----------------

def rule_for(date_str):
    wd = dt.date.fromisoformat(date_str).weekday()
    for d, min_start in DAY_RULES:
        if d == wd:
            return min_start
    return None


def valid(s):
    m = rule_for(s["date"])
    return (m is not None and hhmm(s["startTime"]) >= m
            and s.get("lessonTypeKey", LESSON_TYPE) == LESSON_TYPE)


def is_pair(a, b):
    return (a["date"] == b["date"] and teacher(a) == teacher(b)
            and hhmm(a["endTime"]) == hhmm(b["startTime"]))


def find_pairs(slots):
    slots = sorted(slots, key=lambda s: (s["date"], s["startTime"]))
    return [(a, b) for a in slots for b in slots if a is not b and is_pair(a, b)]


def plan(all_slots, skip=frozenset()):
    """Decide qué reservar en este instante. Devuelve (lista de slots a reservar, días ya completos)."""
    mine = [s for s in all_slots if s.get("isReservedByMe")]
    free = [s for s in all_slots if not s.get("isReservedByMe") and valid(s) and s["id"] not in skip]

    done_days = {a["date"] for a, _ in find_pairs(mine)}
    need = DAYS_WANTED - len(done_days)
    if need <= 0:
        return [], done_days

    to_book, used_days = [], set()

    # 1) Días con 1 sola clase mía (pareja a medias): completar con la de antes/después del mismo profe.
    for m in mine:
        d = m["date"]
        if d in done_days or d in used_days or rule_for(d) is None or need <= 0:
            continue
        mate = next((f for f in free if is_pair(m, f) or is_pair(f, m)), None)
        if mate:
            to_book.append(mate); used_days.add(d); need -= 1

    # 2) Días nuevos por orden de prioridad.
    week_start = dt.date.fromisoformat(WEEK_FROM)
    for wd, _ in DAY_RULES:
        if need <= 0:
            break
        d = (week_start + dt.timedelta(days=(wd - week_start.weekday()) % 7)).isoformat()
        if d in done_days or d in used_days or any(m["date"] == d for m in mine):
            continue
        pairs = find_pairs([f for f in free if f["date"] == d])
        if pairs:
            a, b = pairs[0]                      # la pareja más temprana del día
            to_book += [a, b]; used_days.add(d); need -= 1

    return to_book, done_days


# ---------------- modos ----------------

def probe():
    fake = {"id": "00000000-0000-0000-0000-000000000000"}
    ok, st, txt = reserve(fake)
    log(f"PROBE reserve -> status {st}: {txt}")
    log("Semana objetivo ahora mismo:", len(fetch_week()), "slots")


def dry():
    load_exit_points()
    log("Puntos de salida cargados:", len(EXIT_POINTS))
    slots = fetch_week()
    to_book, done = plan(slots)
    log(f"Slots semana: {len(slots)} · días ya completos: {sorted(done)}")
    log("Reservaría:", [label(s) for s in to_book] or "nada (todavía no hay huecos válidos)")


def test():
    day = (os.environ.get("TEST_DATE") or "").strip()
    if not day:
        raise SystemExit("Falta TEST_DATE (formato 2026-10-07)")
    load_exit_points()
    r = S.get(f"{BASE}/v1/student/lessons/calendar", params={"from": day, "to": day}, timeout=10)
    r.raise_for_status()
    slots = r.json().get("slots", [])
    free = sorted([s for s in slots if not s.get("isReservedByMe")], key=lambda s: s["startTime"])
    log(f"TEST {day}: {len(slots)} slots, {len(free)} libres:", [label(s) for s in free])
    if not free:
        log("No hay huecos libres ese día, no reservo nada.")
        return
    s = free[0]
    t = time.time()
    ok, st, txt = reserve(s)
    log(f"TEST reserva {'OK' if ok else 'FALLO'} {label(s)} [{st}] en {int((time.time()-t)*1000)} ms -> {txt}")
    send_email(f"SNIPER Autius TEST: {'OK' if ok else 'FALLO'}", f"{label(s)}\n[{st}] {txt}")


def live():
    t0 = time.time()
    S.get(f"{BASE}/auth/get-session", timeout=10)  # calienta conexión y renueva sesión
    load_exit_points()
    booked_log, last_beat, fails = [], 0, {}
    while True:
        if time.time() - t0 > RUN_MINUTES * 60:
            log(f"Pasaron {RUN_MINUTES:g} min, salgo.")
            send_email("SNIPER Autius: terminado", "Resumen:\n" + ("\n".join(booked_log) or "No se reservó nada."))
            break
        try:
            slots = fetch_week()
            skip = {k for k, n in fails.items() if n >= 3}
            to_book, done = plan(slots, skip)
            if len(done) >= DAYS_WANTED:
                log("¡Objetivo cumplido!", sorted(done))
                break
            if to_book:
                log("Reservando:", [label(s) for s in to_book])
                with ThreadPoolExecutor(max_workers=len(to_book)) as ex:
                    results = list(ex.map(reserve, to_book))
                lines = []
                for s, (ok, st, txt) in zip(to_book, results):
                    lines.append(f"{'OK ' if ok else 'FALLO'} {label(s)}  [{st}] {'' if ok else txt}")
                    log(lines[-1])
                    if not ok:
                        fails[s["id"]] = fails.get(s["id"], 0) + 1
                booked_log += lines
                if any(ok for ok, _, _ in results):
                    send_email("SNIPER Autius: reservas hechas",
                               "\n".join(lines) + "\n\nRevisa: https://app.autius.com/calendario")
                time.sleep(0.2)  # re-evaluar casi inmediatamente
                continue
            if time.time() - last_beat > 60:
                log(f"Vigilando… slots en la semana: {len(slots)}")
                last_beat = time.time()
        except requests.RequestException as e:
            log("Error red:", e)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    {"probe": probe, "dry": dry, "live": live, "test": test}.get(MODE, live)()
