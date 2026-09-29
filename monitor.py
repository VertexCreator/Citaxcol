#!/usr/bin/env python3
"""Monitor de citas del Consulado General de Colombia en Caracas (Microsoft Bookings).

Consulta la API pública de Bookings, detecta huecos AVAILABLE nuevos y avisa por
ntfy.sh (móvil), Telegram (opcional) y notificación de macOS.

Uso:
  python3 monitor.py --list                      # ver servicios disponibles
  python3 monitor.py --once                      # una sola consulta
  python3 monitor.py                             # bucle continuo (cada ~5 min)
  python3 monitor.py --services PASAPORTE "Cédula Renovación"

Variables de entorno:
  NTFY_TOPIC            tema de ntfy.sh (p. ej. citas-consulado-x7k2p9)
  TELEGRAM_TOKEN, TELEGRAM_CHAT_ID   (opcional)
"""
import argparse
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

BUSINESS = "Atencinalpblico@cancilleria.gov.co"
API = f"https://bookings.cloud.microsoft/BookingsService/api/V1/bookingBusinessesc2/{BUSINESS}"
PAGE = f"https://bookings.cloud.microsoft/book/{BUSINESS}/"
TZ = "Venezuela Standard Time"
STATE_FILE = Path(__file__).with_name("state.json")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def call(path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{API}/{path}",
        data=data,
        method="POST" if data is not None else "GET",
        headers={"Content-Type": "application/json", "User-Agent": UA},
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)


def parse_duration(iso):
    """ISO-8601 'P7D', 'PT12H', 'PT20M' -> timedelta."""
    m = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?", iso or "")
    if not m:
        return timedelta(days=7)
    d, h, mi = (int(x or 0) for x in m.groups())
    return timedelta(days=d, hours=h, minutes=mi)


def get_services():
    services = call("services", {})["service"]
    return [s for s in services if not s.get("isHiddenFromCustomers")]


def get_all_staff():
    return [s["id"] for s in call("staffmembers")["staffMembers"]]


def find_slots(service, all_staff):
    """Devuelve el conjunto de huecos AVAILABLE para un servicio."""
    policy = service.get("bookingsSchedulingPolicy") or {}
    advance = parse_duration(policy.get("maximumAdvance"))
    start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + advance + timedelta(days=1)
    # solo funcionarios que existen: los IDs huérfanos hacen la consulta muy lenta
    staff = [x for x in (service.get("staffMemberIds") or []) if x in all_staff] or all_staff
    resp = call("GetStaffAvailability", {
        "serviceId": service["serviceId"],
        "staffIds": staff,
        "startDateTime": {"dateTime": start.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": TZ},
        "endDateTime": {"dateTime": end.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": TZ},
    })
    slots = set()
    for member in resp.get("staffAvailabilityResponse", []):
        for item in member.get("availabilityItems", []):
            # Cualquier estado distinto de ocupado/fuera de oficina se trata como hueco
            if item["status"] not in ("BOOKINGSAVAILABILITYSTATUS_BUSY",
                                      "BOOKINGSAVAILABILITYSTATUS_OUT_OF_OFFICE"):
                s = item["startDateTime"]["dateTime"][:16].replace("T", " ")
                e = item["endDateTime"]["dateTime"][11:16]
                slots.add(f"{s}-{e}")
    return slots


def notify(title, body):
    topic = os.environ.get("NTFY_TOPIC")
    if topic:
        try:
            req = urllib.request.Request(
                f"https://ntfy.sh/{topic}",
                data=body.encode(),
                headers={"Title": title.encode("utf-8"), "Priority": "urgent",
                         "Tags": "rotating_light", "Click": PAGE},
            )
            urllib.request.urlopen(req, timeout=15)
        except Exception as e:
            log(f"Error ntfy: {e}")
    token, chat = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat:
        try:
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data=json.dumps({"chat_id": chat, "text": f"{title}\n\n{body}\n\n{PAGE}"}).encode(),
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=15)
        except Exception as e:
            log(f"Error Telegram: {e}")
    if sys.platform == "darwin":
        subprocess.run(["osascript", "-e",
                        f'display notification {json.dumps(body[:200], ensure_ascii=False)} with title {json.dumps(title, ensure_ascii=False)} sound name "Glass"'],
                       check=False)


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def check(wanted):
    services = get_services()
    if wanted:
        norm = lambda t: t.strip().lower()
        # coincidencia exacta primero; si no hay, por subcadena
        exact = [s for s in services if any(norm(w) == norm(s["title"]) for w in wanted)]
        services = exact or [s for s in services if any(norm(w) in norm(s["title"]) for w in wanted)]
        if not services:
            log(f"Ningún servicio coincide con {wanted}. Usa --list.")
            return
    all_staff = get_all_staff()
    state = load_state()
    new_state, alerts = {}, []
    for svc in services:
        title = svc["title"].strip()
        try:
            try:
                slots = find_slots(svc, all_staff)
            except Exception:
                time.sleep(10)
                slots = find_slots(svc, all_staff)  # un reintento
        except Exception as e:
            log(f"{title}: error {e}")
            new_state[title] = state.get(title, [])
            continue
        new = sorted(slots - set(state.get(title, [])))
        new_state[title] = sorted(slots)
        log(f"{title}: {len(slots)} huecos" + (f" ({len(new)} nuevos)" if new else ""))
        if new:
            alerts.append(f"{title}:\n" + "\n".join(f"  • {s}" for s in new[:10]))
        time.sleep(1)  # no saturar
    STATE_FILE.write_text(json.dumps(new_state, ensure_ascii=False, indent=1))
    if alerts:
        notify("¡Citas disponibles en el Consulado!", "\n\n".join(alerts))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--services", nargs="*", help="Filtrar por nombre (subcadena)")
    ap.add_argument("--interval", type=int, default=300, help="Segundos entre consultas (def. 300)")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--test-notify", action="store_true")
    a = ap.parse_args()

    if a.list:
        for s in get_services():
            p = s.get("bookingsSchedulingPolicy") or {}
            print(f"- {s['title'].strip():50s} antelación máx {p.get('maximumAdvance')}, mín {p.get('minimumLeadTime')}")
        return
    if a.test_notify:
        notify("Prueba monitor consulado", "Si ves esto, las notificaciones funcionan ✅")
        return
    while True:
        try:
            check(a.services)
        except Exception as e:
            log(f"Error general: {e}")
        if a.once:
            break
        # jitter para no consultar siempre en el mismo segundo
        time.sleep(a.interval + random.randint(-30, 30))


if __name__ == "__main__":
    main()
