#!/usr/bin/env python3
"""
DGP Verdeellijst — cloud-agent (draait op GitHub Actions, cloud/altijd-aan).
Elke run:
  1) verwerkt Telegram-commando's van Coen (vanaf zijn telefoon):
       /status    -> live-link + sync + hoeveel afgevinkt
       /reset      -> ALLES OP NUL (wist afvinken + notities, vraagt bevestiging)
       /reset ja   -> voert de reset echt uit
       /rollback   -> zet de app terug naar de vorige versie
       /help       -> lijst commando's
  2) test alle verbindingen (live-link, kaart, Firestore round-trip) en pingt
     Telegram alleen bij een ECHTE storing en bij herstel (state-change).
State + Telegram-offset staan in Firestore _health/state (app luistert daar niet).
Alleen berichten van CHAT_ID worden uitgevoerd (niemand anders kan commanderen).
"""
import os, json, time, subprocess, urllib.request, urllib.parse

TOK  = os.environ["TG_TOKEN"]
CHAT = os.environ["TG_CHAT"]
KEY  = "AIzaSyD5TV_5VNJAwogO3OqmYydR63NpbD6IHrA"
BASE = "https://firestore.googleapis.com/v1/projects/dgp-verdeellijst/databases/(default)/documents"
LIVE = "https://dgpverdeellijst.github.io/index.html"
MAP  = "https://dgpverdeellijst.github.io/map.jpg"

def http(url, method="GET", data=None, timeout=25):
    req = urllib.request.Request(url, data=(json.dumps(data).encode() if data else None), method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        return urllib.request.urlopen(req, timeout=timeout).read().decode()
    except Exception as e:
        return f"__ERR__{e}"

def tg(method, params):
    return http(f"https://api.telegram.org/bot{TOK}/{method}", "POST", params)

def send(text):
    tg("sendMessage", {"chat_id": CHAT, "text": text})

# ---- Firestore helpers ----
def fs_get(path):
    r = http(f"{BASE}/{path}?key={KEY}")
    try: return json.loads(r)
    except: return {}

def fs_set(path, fields):
    return http(f"{BASE}/{path}?key={KEY}", "PATCH", {"fields": fields})

def fs_del(path):
    return http(f"{BASE}/{path}?key={KEY}", "DELETE")

def list_docs(col):
    out, tok = [], None
    while True:
        u = f"{BASE}/{col}?key={KEY}&pageSize=300" + (f"&pageToken={tok}" if tok else "")
        d = json.loads(http(u) or "{}")
        out += d.get("documents", [])
        tok = d.get("nextPageToken")
        if not tok: break
    return out

def state_doc():
    d = fs_get("_health/state").get("fields", {})
    return d.get("status", {}).get("stringValue", "up"), int(d.get("offset", {}).get("integerValue", "0") or 0)

def save_state(status, offset):
    fs_set("_health/state", {"status": {"stringValue": status}, "offset": {"integerValue": str(offset)}})

# ---- commando's ----
def cmd_status():
    code = subprocess.run(["curl","-s","-o","/dev/null","-w","%{http_code}", LIVE+"?cb="+str(int(time.time()))],
                          capture_output=True, text=True).stdout.strip()
    mapc = subprocess.run(["curl","-s","-o","/dev/null","-w","%{http_code}", MAP], capture_output=True, text=True).stdout.strip()
    # firestore round-trip
    fs_set("_health/probe", {"v": {"stringValue": "g"}})
    rt = fs_get("_health/probe").get("fields", {}).get("v", {}).get("stringValue")
    fs_del("_health/probe")
    docs = list_docs("mat")
    g = sum(1 for x in docs if x.get("fields", {}).get("v", {}).get("stringValue") == "g")
    n = sum(1 for x in docs if x.get("fields", {}).get("v", {}).get("stringValue") == "n")
    notes = len(list_docs("note"))
    ok = code == "200" and mapc == "200" and rt == "g"
    send(f"{'✅' if ok else '🚨'} DGP status:\n"
         f"- live-link: HTTP {code}\n- kaart: HTTP {mapc}\n- live-sync (Firestore): {'ok' if rt=='g' else 'FOUT'}\n"
         f"- afgevinkt: {len(docs)} ({g} gedaan, {n} niet compleet) · notities: {notes}\n\n"
         f"Link: https://dgpverdeellijst.github.io/")

def cmd_reset(confirmed):
    if not confirmed:
        send("⚠️ /reset zet ALLES op nul (wist alle afvinken + notities bij de hele crew).\n"
             "Weet je het zeker? Stuur dan:  /reset ja")
        return
    for col in ("mat", "note"):
        for x in list_docs(col):
            fs_del(x["name"].split("/documents/")[1])
    send("✅ Alles op nul gezet. De crew begint weer bij 0/… (ververst vanzelf op alle telefoons).")

def cmd_rollback():
    subprocess.run(["git", "config", "user.email", "dgpverdeellijst@users.noreply.github.com"])
    subprocess.run(["git", "config", "user.name", "dgpverdeellijst"])
    log = subprocess.run(["git", "log", "--format=%H", "-n", "2", "--", "index.html"],
                         capture_output=True, text=True).stdout.split()
    if len(log) < 2:
        send("↩️ Rollback kan niet: geen vorige versie van de app gevonden.")
        return
    prev = log[1]
    prevhtml = subprocess.run(["git", "show", f"{prev}:index.html"], capture_output=True).stdout
    open("index.html", "wb").write(prevhtml)
    subprocess.run(["git", "add", "index.html"])
    subprocess.run(["git", "commit", "-m", "Rollback via Telegram: app terug naar vorige versie"])
    r = subprocess.run(["git", "push"], capture_output=True, text=True)
    send("↩️ App teruggezet naar de vorige versie." if r.returncode == 0
         else f"↩️ Rollback lokaal gedaan maar push faalde:\n{r.stderr[:300]}")

def handle(text):
    t = text.strip().lower()
    if t.startswith("/status"):    cmd_status()
    elif t.startswith("/reset"):   cmd_reset("ja" in t)
    elif t.startswith("/rollback"): cmd_rollback()
    elif t.startswith("/help") or t.startswith("/start"):
        send("DGP Verdeellijst — commando's:\n"
             "/status — link + sync + hoeveel afgevinkt\n"
             "/reset — alles op nul (vraagt bevestiging)\n"
             "/rollback — app terug naar vorige versie\n"
             "Je krijgt automatisch een ping als er iets stuk gaat.")

# ---- main ----
def process_commands(offset):
    r = http(f"https://api.telegram.org/bot{TOK}/getUpdates?offset={offset+1}&timeout=0&allowed_updates=%5B%22message%22%5D")
    try: ups = json.loads(r).get("result", [])
    except: return offset
    for u in ups:
        offset = max(offset, u["update_id"])
        msg = u.get("message") or {}
        if str(msg.get("chat", {}).get("id")) != str(CHAT):   # alleen Coen mag commanderen
            continue
        txt = msg.get("text", "")
        if txt.startswith("/"):
            try: handle(txt)
            except Exception as e: send(f"Fout bij '{txt}': {e}")
    return offset

def monitor(prev_status):
    fails = []
    for url, label in ((LIVE, "live-link"), (MAP, "kaart")):
        code = subprocess.run(["curl","-s","-o","/dev/null","-w","%{http_code}", url+"?cb="+str(int(time.time()))],
                              capture_output=True, text=True).stdout.strip()
        if code != "200": fails.append(f"{label} HTTP {code}")
    if "syncSpecial" not in (http(LIVE+"?cb="+str(int(time.time()))) or ""):
        fails.append("app draait niet (build/marker mist)")
    fs_set("_health/probe", {"v": {"stringValue": "g"}})
    if fs_get("_health/probe").get("fields", {}).get("v", {}).get("stringValue") != "g":
        fails.append("Firestore live-sync onbereikbaar")
    fs_del("_health/probe")
    now = "up" if not fails else "down"
    if now != prev_status:
        if fails: send("🚨 DGP Verdeellijst PROBLEEM:\n- " + "\n- ".join(fails) +
                       "\n\n/status voor details · /rollback om terug te zetten")
        elif prev_status == "down": send("✅ DGP Verdeellijst werkt weer.")
    return now

if __name__ == "__main__":
    status, offset = state_doc()
    offset = process_commands(offset)
    status = monitor(status)
    save_state(status, offset)
