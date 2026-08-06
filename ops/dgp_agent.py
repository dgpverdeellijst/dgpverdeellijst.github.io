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
import os, json, time, subprocess, urllib.request, urllib.parse, re, difflib

TOK  = os.environ["TG_TOKEN"]
CHAT = os.environ["TG_CHAT"]
KEY  = "AIzaSyD5TV_5VNJAwogO3OqmYydR63NpbD6IHrA"
BASE = "https://firestore.googleapis.com/v1/projects/dgp-verdeellijst/databases/(default)/documents"
LIVE = "https://dgpverdeellijst.github.io/index.html"
MAP  = "https://dgpverdeellijst.github.io/map.jpg"

def http(url, method="GET", data=None, timeout=25):
    # via curl -> werkt betrouwbaar op zowel de cloud-runner als lokaal (geen SSL-gedoe)
    args = ["curl", "-s", "--max-time", str(timeout), "-X", method]
    if data is not None:
        args += ["-H", "Content-Type: application/json", "-d", json.dumps(data)]
    args.append(url)
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout + 5).stdout
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

# ---- data-edits vanaf de telefoon ----
def _git_setup():
    subprocess.run(["git", "config", "user.email", "dgpverdeellijst@users.noreply.github.com"])
    subprocess.run(["git", "config", "user.name", "dgpverdeellijst"])

def repo_bars():
    h = open("index.html", encoding="utf-8").read()
    i0 = h.find("window.BARS"); i1 = h.find("const MAPIMG", i0)
    data = json.loads(re.search(r"window\.BARS\s*=\s*(\[.*\]);", h[i0:i1], re.S).group(1))
    return h, i0, i1, data

def js_broken(path="index.html"):
    """Lege string = OK. Anders reden: JS-syntaxfout of window.BARS parse-t niet."""
    try:
        h = open(path, encoding="utf-8").read()
        scripts = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", h, re.S)
        open("/tmp/_chk.js", "w").write("\n;\n".join(scripts))
        if subprocess.run(["node", "--check", "/tmp/_chk.js"], capture_output=True).returncode != 0:
            return "JS-syntaxfout"
        i0 = h.find("window.BARS"); i1 = h.find("const MAPIMG", i0)
        json.loads(re.search(r"window\.BARS\s*=\s*(\[.*\]);", h[i0:i1], re.S).group(1))
        return ""
    except Exception as e:
        return f"data kapot ({e})"

def write_bars(h, i0, i1, data, msg):
    DATA = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    open("index.html", "w", encoding="utf-8").write(h[:i0] + "window.BARS = " + DATA + ";\n" + h[i1:])
    bad = js_broken()                         # nooit een kapotte versie pushen
    if bad:
        subprocess.run(["git", "checkout", "--", "index.html"])
        return f"__BAD__{bad}"
    _git_setup()
    subprocess.run(["git", "add", "index.html"])
    subprocess.run(["git", "commit", "-m", msg])
    return "__OK__" if subprocess.run(["git", "push"], capture_output=True, text=True).returncode == 0 else "__PUSHFAIL__"

def auto_heal():
    """Als de gedeployde app-code kapot is: automatisch terug naar de vorige werkende versie."""
    bad = js_broken()
    if not bad:
        return None
    log = subprocess.run(["git", "log", "--format=%H", "-n", "2", "--", "index.html"],
                         capture_output=True, text=True).stdout.split()
    if len(log) < 2:
        return f"app kapot ({bad}) maar geen vorige versie om terug te zetten"
    prev = subprocess.run(["git", "show", f"{log[1]}:index.html"], capture_output=True).stdout
    open("index.html", "wb").write(prev)
    if js_broken():                            # vorige versie ook kapot? niet pushen
        subprocess.run(["git", "checkout", "--", "index.html"])
        return f"app kapot ({bad}) en vorige versie óók — handmatig nodig"
    _git_setup()
    subprocess.run(["git", "add", "index.html"])
    subprocess.run(["git", "commit", "-m", "Auto-herstel: kapotte versie teruggezet naar vorige werkende"])
    subprocess.run(["git", "push"], capture_output=True, text=True)
    send(f"🛠️ Kapotte app-versie gedetecteerd en AUTOMATISCH teruggezet naar de vorige werkende versie.\nReden: {bad}")
    return "hersteld"

def find_bar(data, name):
    name = name.strip()
    for b in data:
        if b["loc"].lower() == name.lower(): return b
    for b in data:
        if name.lower() in b["loc"].lower(): return b
    m = difflib.get_close_matches(name, [b["loc"] for b in data], n=1, cutoff=0.4)
    return next((b for b in data if b["loc"] == m[0]), None) if m else None

def cmd_set(arg):
    p = [x.strip() for x in arg.split("|")]
    if len(p) < 4:
        send("Gebruik:  /set <bar> | <categorie> | <materiaal> | <aantal>\n"
             "bijv:  /set F14A1 | afval | 1100Ltr rest | 5"); return
    h, i0, i1, data = repo_bars()
    b = find_bar(data, p[0])
    if not b: send(f"Bar '{p[0]}' niet gevonden."); return
    cats = sorted({t["list"] for x in data for t in x["tasks"]})
    cm = difflib.get_close_matches(p[1], cats, n=1, cutoff=0.3); cat = cm[0] if cm else p[1]
    mats = sorted({t["material"] for x in data for t in x["tasks"] if t["list"] == cat})
    mm = difflib.get_close_matches(p[2], mats, n=1, cutoff=0.5); mat = mm[0] if mm else p[2]
    hit = False
    for t in b["tasks"]:
        if t["list"] == cat and t["material"].lower() == mat.lower(): t["qty"] = p[3]; hit = True
    if not hit: b["tasks"].append({"list": cat, "material": mat, "qty": p[3]})
    _wrep(write_bars(h, i0, i1, data, f"Telefoon-edit: {b['loc']} {cat}/{mat}={p[3]}"),
          f"{b['loc']} → [{cat}] {mat} = {p[3]}")

def cmd_rm(arg):
    p = [x.strip() for x in arg.split("|")]
    if len(p) < 2:
        send("Gebruik:  /rm <bar> | <materiaal>\nbijv:  /rm F14A1 | Dixi"); return
    h, i0, i1, data = repo_bars()
    b = find_bar(data, p[0])
    if not b: send(f"Bar '{p[0]}' niet gevonden."); return
    mats = [t["material"] for t in b["tasks"]]
    mm = difflib.get_close_matches(p[1], mats, n=1, cutoff=0.4)
    target = mm[0] if mm else next((m for m in mats if p[1].lower() in m.lower()), None)
    if not target: send(f"Materiaal '{p[1]}' niet op {b['loc']}. Aanwezig: {', '.join(mats)}"); return
    b["tasks"] = [t for t in b["tasks"] if t["material"] != target]
    _wrep(write_bars(h, i0, i1, data, f"Telefoon-edit: {b['loc']} - {target} verwijderd"),
          f"{target} verwijderd van {b['loc']}")

def _wrep(r, okmsg):
    if r == "__OK__":            send("✅ " + okmsg + "  (live over ~1 min)")
    elif r.startswith("__BAD__"): send("⚠️ Niet doorgevoerd — zou de app kapotmaken: " + r[7:])
    else:                        send("⚠️ Wijziging gemaakt maar push naar de server faalde.")

def cmd_melding(arg):
    if not arg.strip(): send("Gebruik:  /melding <omschrijving>\nbijv:  /melding F14A klopt niet, mist afvalzakken"); return
    fs_set(f"_reports/{int(time.time())}", {"tekst": {"stringValue": arg.strip()},
                                            "tijd": {"stringValue": time.strftime('%F %T')}})
    send("📝 Melding opgeslagen. Ik pak 'm op en zoek het goed uit (met de agents) zodra ik weer op de laptop zit. "
         "Voor een snelle directe fix kun je /set of /rm gebruiken.")

def handle(text):
    t = text.strip(); tl = t.lower()
    if tl.startswith("/status"):    cmd_status()
    elif tl.startswith("/reset"):   cmd_reset("ja" in tl)
    elif tl.startswith("/rollback"): cmd_rollback()
    elif tl.startswith("/set "):    cmd_set(t[5:])
    elif tl.startswith("/rm "):     cmd_rm(t[4:])
    elif tl.startswith("/melding"): cmd_melding(t[8:])
    elif tl.startswith("/help") or tl.startswith("/start"):
        send("DGP Verdeellijst — commando's:\n\n"
             "🔎 /status — link + sync + hoeveel afgevinkt\n"
             "📝 /melding <tekst> — meld dat iets niet klopt (ik fix het goed uit)\n"
             "✏️ /set <bar> | <categorie> | <materiaal> | <aantal> — materiaal zetten/wijzigen\n"
             "🗑️ /rm <bar> | <materiaal> — materiaal weghalen\n"
             "↩️ /rollback — app terug naar vorige versie\n"
             "🧨 /reset — alles op nul (dan /reset ja)\n\n"
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
    auto_heal()                     # kapotte versie? automatisch terugzetten
    status = monitor(status)
    save_state(status, offset)
