"""Una pasada: mira el calendario de Autius y manda email si hay huecos nuevos.
Lo ejecuta GitHub Actions cada ~5 min. El estado (huecos ya avisados) va en state.json."""
import os, json, smtplib, datetime as dt
from email.message import EmailMessage
import requests

BASE = "https://api.autius.com/api"
DAYS_AHEAD = 21
STATE_FILE = "state.json"

COOKIE = os.environ["AUTIUS_COOKIE"]
GMAIL_USER = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
EMAIL_TO = os.environ.get("EMAIL_TO") or GMAIL_USER

HEADERS = {
    "Cookie": COOKIE,
    "Accept": "application/json",
    "Origin": "https://app.autius.com",
    "Referer": "https://app.autius.com/",
}


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"seen": [], "expired_notified": False}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def send_email(subject, body):
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, GMAIL_USER, EMAIL_TO
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        smtp.send_message(msg)


def label(s):
    ins = s.get("instructor") or {}
    return (f"{s['date']} {s['startTime'][:5]}-{s['endTime'][:5]} · "
            f"{ins.get('firstName','')} {ins.get('lastName','')} · {s.get('lessonTypeName','')}")


def main():
    state = load_state()

    # 1) Mantener viva la sesión (Autius la renueva 7 días cada vez que se usa).
    requests.get(f"{BASE}/auth/get-session", headers=HEADERS, timeout=20)

    # 2) Pedir el calendario.
    today = dt.date.today()
    params = {"from": today.isoformat(), "to": (today + dt.timedelta(days=DAYS_AHEAD)).isoformat()}
    r = requests.get(f"{BASE}/v1/student/lessons/calendar", headers=HEADERS, params=params, timeout=20)

    if r.status_code in (401, 403):
        if not state.get("expired_notified"):
            send_email("Autius: sesión caducada",
                       "Copia de nuevo la cookie y actualiza el secret AUTIUS_COOKIE en GitHub.")
            state["expired_notified"] = True
            save_state(state)
        print("Sesión caducada")
        return
    r.raise_for_status()
    state["expired_notified"] = False

    slots = r.json().get("slots", [])
    free = [s for s in slots if not s.get("isReservedByMe")]
    seen = set(state.get("seen", []))
    new = [s for s in free if s["id"] not in seen]

    if new:
        body = "\n".join(label(s) for s in new) + "\n\nReserva: https://app.autius.com/calendario"
        send_email(f"Autius: {len(new)} hueco(s) libre(s)", body)

    # Solo recordamos los huecos que siguen libres: si uno desaparece y vuelve, se avisa otra vez.
    state["seen"] = [s["id"] for s in free]
    save_state(state)
    print(f"OK · libres: {len(free)} · nuevos avisados: {len(new)}")  # sin detalles en el log público


if __name__ == "__main__":
    main()
