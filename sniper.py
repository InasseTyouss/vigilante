"""
SNIPER Autius: consulta el calendario cada segundo y, en cuanto salen huecos, reserva.

Objetivos (semana 19-23 oct 2026), se persiguen EN PARALELO:
  TARDES : 2 días, cada uno con 2 clases seguidas del mismo profe.
           Prioridad: martes 20 (>=18:45), miércoles 21 (>=18:45), viernes 23 (>=16:00).
           Lunes (festivo) y jueves no.
           El viernes, si hay 3 seguidas con el mismo profe, coge las 3.
  MAÑANAS: 2 dobles 07:30-09:00 (07:30 + 08:15), martes/miércoles/viernes, mismo profe en las dos.
           Preferencia DAVID PIZARRO; si ese día no hay con él, vale otro profe.
Se reserva al instante lo que vaya saliendo (los días se abren a mano, poco a poco).

Modos (variable MODE):
  probe -> NO reserva nada. Reserva falsa a un id inexistente para ver qué pide la API.
  dry   -> NO reserva nada. Muestra qué reservaría ahora mismo.
  live  -> Vigila cada segundo durante RUN_MINUTES y reserva.
  test  -> RESERVA DE VERDAD 1 sola clase (la primera libre) del día TEST_DATE.
"""
import os, time, smtplib, datetime as dt
from concurrent.futures import ThreadPoolExecutor
from email.message import EmailMessage
from zoneinfo import ZoneInfo
import requests

# ---------------- CONFIGURACIÓN ----------------
TZ = ZoneInfo("Europe/Madrid")
WEEK_FROM, WEEK_TO = "2026-10-19", "2026-10-23"
# Cada objetivo: días (0=lunes) en orden de prioridad con su hora mínima de inicio,
# horas de inicio exactas permitidas (None = cualquiera), profesor (None = cualquiera) y nº de días.
# "prefer": profesor preferido (si no hay, vale otro). "max_block": días (0=lunes) en los que
# se aceptan más de 2 clases seguidas (ej. viernes tarde: hasta 3).
GOALS = [
    {"name": "TARDES", "days": [(1, "18:45"), (2, "18:45"), (4, "16:00")],
     "starts": None, "teacher": None, "prefer": None, "max_block": {4: 3}, "days_wanted": 2},
    {"name": "MAÑANAS", "days": [(1, "07:30"), (2, "07:30"), (4, "07:30")],
     "starts": {"07:30", "08:15"}, "teacher": None, "prefer": "DAVID PIZARRO", "max_block": {},
     "days_wanted": 2},
]
LESSON_TYPE = "class_45"          # clase individual de 45 min
POLL_SECONDS = 1.0
RUN_MINUTES = float(__import__("os").environ.get("RUN_MINUTES") or 90)   # cuánto rato vigila desde que lo lanzas
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

def min_start(goal, date_str):
    wd = dt.date.fromisoformat(date_str).weekday()
    return next((m for d, m in goal["days"] if d == wd), None)


def valid(goal, s):
    m = min_start(goal, s["date"])
    st = hhmm(s["startTime"])
    return (m is not None and st >= m
            and (goal["starts"] is None or st in goal["starts"])
            and (goal["teacher"] is None or teacher(s).upper() == goal["teacher"])
            and s.get("lessonTypeKey", LESSON_TYPE) == LESSON_TYPE)


def is_pair(a, b):
    return (a["date"] == b["date"] and teacher(a) == teacher(b)
            and hhmm(a["endTime"]) == hhmm(b["startTime"]))


def find_pairs(slots):
    slots = sorted(slots, key=lambda s: (s["date"], s["startTime"]))
    return [(a, b) for a in slots for b in slots if a is not b and is_pair(a, b)]


def best_block(goal, day_slots):
    """Mejor bloque de clases seguidas del mismo profe en un día: profe preferido > más largo > más temprano."""
    if not day_slots:
        return []
    wd = dt.date.fromisoformat(day_slots[0]["date"]).weekday()
    max_len = goal["max_block"].get(wd, 2)
    slots = sorted(day_slots, key=lambda s: s["startTime"])
    blocks = []
    for a in slots:                                   # cadena que empieza en a
        chain = [a]
        while len(chain) < max_len:
            nxt = next((s for s in slots if is_pair(chain[-1], s)), None)
            if not nxt:
                break
            chain.append(nxt)
        if len(chain) >= 2:
            blocks.append(chain)
    if not blocks:
        return []
    pref = goal.get("prefer")
    blocks.sort(key=lambda c: (0 if pref and teacher(c[0]).upper() == pref else 1, -len(c), c[0]["startTime"]))
    return blocks[0]


def date_for(wd):
    start = dt.date.fromisoformat(WEEK_FROM)
    return (start + dt.timedelta(days=(wd - start.weekday()) % 7)).isoformat()


def plan_goal(goal, all_slots, skip):
    """Qué reservar ahora para este objetivo. Devuelve (slots a reservar, días ya completos)."""
    mine = [s for s in all_slots if s.get("isReservedByMe") and valid(goal, s)]
    free = [s for s in all_slots if not s.get("isReservedByMe") and valid(goal, s) and s["id"] not in skip]
    done = {a["date"] for a, _ in find_pairs(mine)}
    need = goal["days_wanted"] - len(done)
    to_book, used = [], set()
    if need <= 0:
        return [], done
    # 1) Días con 1 sola clase mía de este objetivo: buscarle compañera.
    for m in mine:
        d = m["date"]
        if need <= 0 or d in done or d in used:
            continue
        mate = next((f for f in free if is_pair(m, f) or is_pair(f, m)), None)
        if mate:
            to_book.append(mate); used.add(d); need -= 1
    # 2) Días nuevos por prioridad.
    for wd, _ in goal["days"]:
        if need <= 0:
            break
        d = date_for(wd)
        if d in done or d in used or any(m["date"] == d for m in mine):
            continue
        block = best_block(goal, [f for f in free if f["date"] == d])
        if block:
            to_book += block; used.add(d); need -= 1
    return to_book, done


def plan(all_slots, skip=frozenset()):
    """Junta todos los objetivos. Devuelve (slots a reservar, {objetivo: días completos}, ¿todo cumplido?)."""
    to_book, status, ids = [], {}, set()
    for g in GOALS:
        tb, done = plan_goal(g, all_slots, skip | ids)
        to_book += tb; ids |= {s["id"] for s in tb}
        status[g["name"]] = sorted(done)
    finished = all(len(status[g["name"]]) >= g["days_wanted"] for g in GOALS)
    return to_book, status, finished


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
    to_book, status, _ = plan(slots)
    log(f"Slots semana: {len(slots)} · completos: {status}")
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
            to_book, status, finished = plan(slots, skip)
            if finished:
                log("¡Todo cumplido!", status)
                send_email("SNIPER Autius: todo cumplido", f"{status}\n\n" + "\n".join(booked_log))
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
