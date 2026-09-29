"""Reelbot : trie les réels / TikTok / YouTube / posts photo envoyés au bot Telegram.

Les liens arrivent de deux façons :
  - dossier inbox/ : déposés instantanément par le relais Cloudflare (worker.js)
  - getUpdates Telegram : ancienne méthode, utilisée tant que le relais n'est pas branché
Chaque lien est analysé par Gemini, rangé dans reels.json (affiché par index.html),
et le message Telegram correspondant est supprimé. Un message de statut suit le tri en direct,
et des alertes ne sont envoyées que quand il faut agir.
"""
import datetime, glob, json, mimetypes, os, re, subprocess, sys, tempfile, time
import requests
from google import genai
from google.genai import types

TG = f"https://api.telegram.org/bot{os.environ['TELEGRAM_TOKEN']}"
ALLOWED = os.environ.get("ALLOWED_CHAT_ID", "").strip()
MODELS = [m for m in [os.environ.get("GEMINI_MODEL", "").strip(),
                      "gemini-3.8-flash", "gemini-3.5-flash", "gemini-3.1-flash-lite"] if m]
MAX_TRIES = 3          # un réel en échec est retenté aux tris suivants, 3 fois maximum
TIME_BUDGET = 20 * 60  # GitHub coupe à 30 min : on s'arrête proprement avant
client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
URL_RE = re.compile(r"https?://\S+")
DATA, STATE, INBOX = "reels.json", "etat.json", "inbox"
CATEGORIES = ["Tuto IA", "Outil IA", "Actu IA", "Business", "Marrant", "Autre"]
owner, _, repo = os.environ.get("GITHUB_REPOSITORY", "/").partition("/")
PAGE_URL = f"https://{owner.lower()}.github.io/{repo}/"
TODAY = datetime.date.today().isoformat()

PROMPT = """Tu analyses un contenu court (vidéo, ou une ou plusieurs images d'un carrousel) sauvegardé par
un jeune qui s'intéresse surtout à l'IA, aux automatisations et aux nouveaux outils.
S'il y a du texte dans les images, lis-le attentivement : c'est souvent là qu'est l'information.
Description d'origine : {desc}
Réponds UNIQUEMENT en JSON, en français, avec ces clés :
- "titre" : titre très bien choisi, précis et accrocheur, qui dit concrètement ce qu'on apprend (max 8 mots)
- "categorie" : EXACTEMENT une parmi {cats}.
   Tuto IA = montre comment faire quelque chose avec l'IA ; Outil IA = présente un outil/appli ;
   Actu IA = nouvelle, annonce ; Business = argent, entrepreneuriat ; Marrant = humour/divertissement.
   Si tu n'es pas sûr, mets "Autre".
- "note" : entier 1 à 5 (5 = très utile et concret, 1 = vide / pur buzz)
- "resume" : 1 à 2 phrases courtes sur ce que le contenu apporte
- "a_retenir" : l'outil, l'astuce ou le lien clé à retenir (ou "" si rien)"""


# ---------------------------------------------------------------- Telegram
def tg(method, **params):
    try:
        return requests.post(f"{TG}/{method}", json=params, timeout=60).json()
    except Exception as e:
        return {"ok": False, "description": str(e)}


# ---------------------------------------------------------------- Erreurs "à action"
class Alert(RuntimeError):
    """Erreur qui demande une action de ta part (ou qui mérite d'être signalée)."""
    def __init__(self, key, short, alert_text):
        super().__init__(short)
        self.key, self.alert_text = key, alert_text


INSTA_BLOCK = ("login", "log in", "rate-limit", "rate limit", "not available", "401", "403",
               "429", "cookies", "checkpoint", "Please wait")
ALERT_INSTA = ("🔒 Instagram me bloque (il demande une connexion).\n"
               "À faire une fois : ajoute tes cookies Instagram dans le secret GitHub INSTAGRAM_COOKIES. "
               "Demande à Claude « ajoute les cookies Insta au reelbot » pour le pas-à-pas.\n"
               "En attendant, les réels bloqués sont gardés et retentés.")
ALERT_QUOTA = ("⛽ Quota Gemini gratuit épuisé pour aujourd'hui.\n"
               "Rien à faire : les réels restants sont gardés et je reprends automatiquement au prochain tri.")
ALERT_KEY = ("🔑 Google refuse la clé Gemini (clé invalide ou supprimée).\n"
             "À faire : crée une nouvelle clé sur aistudio.google.com, puis remplace le secret GitHub GEMINI_API_KEY.")


# ---------------------------------------------------------------- Téléchargement
def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=300)


def with_cookies(cmd, flag="--cookies"):
    if os.path.exists("cookies.txt"):
        cmd[1:1] = [flag, "cookies.txt"]
    return cmd


def download(url, tmp):
    """Renvoie (liste de fichiers médias, description). Vidéo -> yt-dlp ; photos / carrousels -> gallery-dl."""
    errors = []
    # 1) vidéo (réel, TikTok…)
    r = run(with_cookies(["yt-dlp", "-q", "--no-playlist", "-f", "best[ext=mp4][height<=720]/best[ext=mp4]/best",
                          "--max-filesize", "150M", "--write-info-json", "-o", f"{tmp}/v.%(ext)s", url]))
    vids = [f for f in glob.glob(f"{tmp}/v.*") if not f.endswith(".json")]
    if vids:
        desc = ""
        for j in glob.glob(f"{tmp}/*.info.json"):
            info = json.load(open(j))
            desc = (info.get("description") or info.get("title") or "")[:1500]
        return vids[:1], desc
    errors.append(r.stderr.strip())

    # 2) photo ou carrousel (plusieurs images, éventuellement des vidéos)
    gdir = f"{tmp}/g"
    r = run(with_cookies(["gallery-dl", "-q", "--write-metadata", "-D", gdir, url], "--cookies"))
    files = sorted(f for f in glob.glob(f"{gdir}/*")
                   if (mimetypes.guess_type(f)[0] or "").split("/")[0] in ("image", "video"))
    if files:
        desc = ""
        for j in sorted(glob.glob(f"{gdir}/*.json")):
            meta = json.load(open(j))
            desc = (meta.get("description") or meta.get("caption") or "")[:1500]
            if desc:
                break
        return files[:10], desc
    errors.append(r.stderr.strip() or r.stdout.strip())

    msg = " | ".join(e.splitlines()[-1] for e in errors if e)[:300] or "aucun média trouvé"
    if "instagram.com" in url and any(k.lower() in msg.lower() for k in INSTA_BLOCK):
        raise Alert("insta", "Instagram demande une connexion (cookies).", ALERT_INSTA)
    raise RuntimeError("téléchargement impossible : " + msg)


# ---------------------------------------------------------------- Analyse Gemini
def upload(path):
    f = client.files.upload(file=path)
    while f.state.name == "PROCESSING":
        time.sleep(3)
        f = client.files.get(name=f.name)
    return f


def analyse(url, on_wait=lambda txt: None):
    with tempfile.TemporaryDirectory() as tmp:
        if "youtube.com" in url or "youtu.be" in url:  # Gemini lit YouTube directement
            parts, desc = [types.Part(file_data=types.FileData(file_uri=url))], ""
        else:
            files, desc = download(url, tmp)
            parts = [upload(p) for p in files]
        prompt = PROMPT.format(desc=desc or "(aucune)", cats=", ".join(CATEGORIES))
        errs = []
        for model in MODELS:
            for attempt in range(3):
                try:
                    resp = client.models.generate_content(
                        model=model, contents=parts + [prompt],
                        config=types.GenerateContentConfig(response_mime_type="application/json"))
                    d = json.loads(resp.text)
                    d = d[0] if isinstance(d, list) else d
                    if d.get("categorie") not in CATEGORIES:
                        d["categorie"] = "Autre"
                    return d
                except Exception as e:
                    msg = str(e)
                    errs.append(msg)
                    print(f"  {model} essai {attempt + 1} : {msg[:200]}")
                    if "API_KEY_INVALID" in msg or "API key not valid" in msg or "PERMISSION_DENIED" in msg:
                        raise Alert("cle", "Clé Gemini refusée.", ALERT_KEY)
                    if "PerDay" in msg or "per day" in msg.lower():
                        break  # quota du jour épuisé pour ce modèle : on passe au suivant sans attendre
                    if any(k in msg for k in ("429", "RESOURCE_EXHAUSTED", "503", "UNAVAILABLE", "500")) and attempt < 2:
                        wait = 30 * (attempt + 1)
                        on_wait(f"Gemini saturé, nouvel essai dans {wait} s…")
                        time.sleep(wait)
                        continue
                    break  # modèle indisponible : suivant
        if errs and all(("429" in e or "RESOURCE_EXHAUSTED" in e) for e in errs):
            raise Alert("quota", "Quota Gemini épuisé, repris au prochain tri.", ALERT_QUOTA)
        raise RuntimeError(f"Gemini : {errs[-1][:200] if errs else '?'}")


# ---------------------------------------------------------------- Sauvegarde GitHub
def git_save(msg):
    """Enregistre tout de suite sur GitHub (la page se met à jour sans attendre la fin du tri)."""
    if not os.environ.get("GITHUB_ACTIONS"):
        return
    g = ["git", "-c", "user.name=reelbot", "-c", "user.email=reelbot@users.noreply.github.com"]
    subprocess.run(g + ["add", "-A", DATA, STATE] + ([INBOX] if os.path.isdir(INBOX) else []), capture_output=True)
    subprocess.run(g + ["add", "-u"], capture_output=True)  # fichiers inbox supprimés
    if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode == 0:
        return
    subprocess.run(g + ["commit", "-q", "-m", msg], capture_output=True)
    for _ in range(3):
        subprocess.run(g + ["pull", "-q", "--rebase"], capture_output=True)
        if subprocess.run(["git", "push", "-q"], capture_output=True).returncode == 0:
            return
        time.sleep(3)


# ---------------------------------------------------------------- Programme principal
def main(state):
    if owner and repo:  # bouton "📂 Réels" en bas de la conversation
        tg("setChatMenuButton", menu_button={"type": "web_app", "text": "📂 Réels", "web_app": {"url": PAGE_URL}})

    reels = json.load(open(DATA, encoding="utf-8")) if os.path.exists(DATA) else []
    known = {r["lien"] for r in reels}
    chat = int(ALLOWED) if ALLOWED else (int(os.environ["CHAT_ID"]) if os.environ.get("CHAT_ID") else state.get("chat"))
    todo_new, to_delete, inbox_files = [], [], sorted(glob.glob(f"{INBOX}/*.json"))

    def take(c, mid, text):
        nonlocal chat
        if not c or (ALLOWED and str(c) != ALLOWED):
            return
        chat = c
        to_delete.append((c, mid))
        for url in URL_RE.findall(text or ""):
            if url not in known:
                known.add(url)
                todo_new.append({"date": TODAY, "lien": url})

    # Liens déposés par le relais Cloudflare
    for f in inbox_files:
        m = json.load(open(f))
        take(m.get("chat"), m.get("message_id"), m.get("text"))
    # Ancienne méthode (si le relais n'est pas branché, sinon Telegram répond une erreur : sans effet)
    updates = tg("getUpdates", timeout=0).get("result", [])
    for u in updates:
        msg = u.get("message") or {}
        take(msg.get("chat", {}).get("id"), msg.get("message_id"),
             (msg.get("text") or "") + " " + (msg.get("caption") or ""))

    todo_retry = [r for r in reels if r.get("categorie") == "Échec" and r.get("essais", 1) < MAX_TRIES]
    todo = todo_retry + todo_new

    # Nettoyage : messages envoyés, ancien statut, fichiers inbox
    for c, mid in to_delete:
        tg("deleteMessage", chat_id=c, message_id=mid)
    if updates:
        tg("getUpdates", offset=updates[-1]["update_id"] + 1, timeout=0)
    for f in inbox_files:
        os.remove(f)
    new_status = int(os.environ["STATUS_ID"]) if os.environ.get("STATUS_ID") else None
    if chat and state.get("status_id") and state["status_id"] != new_status:
        tg("deleteMessage", chat_id=chat, message_id=state["status_id"])
    state["status_id"] = new_status
    if not chat:
        print("Aucune conversation connue.")
        return
    state["chat"] = chat

    # ---- alertes : un message qui reste tant que le problème n'est pas réglé
    alerts = state.setdefault("alerts", {})

    def alert(key, text):
        a = alerts.get(key)
        if a and a.get("date") == TODAY:
            return  # déjà prévenu aujourd'hui
        if a and a.get("mid"):
            tg("deleteMessage", chat_id=chat, message_id=a["mid"])
        r = tg("sendMessage", chat_id=chat, text=text)
        alerts[key] = {"date": TODAY, "mid": r.get("result", {}).get("message_id")}

    def resolve(key, text=None):
        a = alerts.pop(key, None)
        if a and a.get("mid"):
            tg("deleteMessage", chat_id=chat, message_id=a["mid"])  # problème réglé : on efface l'alerte

    # ---- statut en direct
    button = {"inline_keyboard": [[{"text": "📂 Ouvrir mes réels", "web_app": {"url": PAGE_URL}}]]} if owner else None

    def status(text):
        params = dict(chat_id=chat, text=text, disable_web_page_preview=True)
        if button:
            params["reply_markup"] = button
        if state.get("status_id"):
            r = tg("editMessageText", message_id=state["status_id"], **params)
            if r.get("ok") or "not modified" in str(r.get("description", "")):
                return
        r = tg("sendMessage", **params)
        if r.get("ok"):
            state["status_id"] = r["result"]["message_id"]

    total = lambda: len([r for r in reels if r.get("categorie") != "Échec"])
    if not todo:
        status(f"✅ Rien à trier : aucun nouveau réel et aucun échec à retenter.\n📊 {total()} réels classés au total.")
        return reels
    status(f"⏳ Tri en cours : {len(todo)} réel(s)\n• {len(todo_new)} nouveau(x)\n• {len(todo_retry)} échec(s) à retenter")

    ok, ko, abandoned, start = [], [], [], time.time()
    worked, failed = set(), set()  # pour effacer les alertes des problèmes réglés
    for n, item in enumerate(todo, 1):
        if time.time() - start > TIME_BUDGET:
            for rest in todo[n - 1:]:
                if rest in todo_new:
                    rest.update(titre="En attente", categorie="Échec", note=0, a_retenir="", essais=0,
                                resume="Pas eu le temps, repris au prochain tri.")
                    reels.append(rest)
            status(f"⏸️ Tri arrêté après 20 min : {len(ok)} classé(s), le reste sera fait au prochain tri.")
            todo = todo[:n - 1]
            break
        head = lambda: f"⏳ Tri en cours : réel {n}/{len(todo)}\n✅ {len(ok)} classé(s)   ⚠️ {len(ko)} échec(s)\n"
        status(head() + "🔎 Analyse en cours…")
        try:
            d = analyse(item["lien"], on_wait=lambda txt: status(head() + "⌛ " + txt))
            for k in ["titre", "categorie", "note", "resume", "a_retenir"]:
                item[k] = d.get(k, "")
            item.pop("essais", None)
            ok.append(item)
            worked.update({"cle", "quota"} | ({"insta"} if "instagram.com" in item["lien"] else set()))
            print("OK", item["lien"], item["titre"])
        except Exception as e:
            is_alert = isinstance(e, Alert)
            # quota épuisé : ça ne compte pas comme un essai raté
            tries = item.get("essais", 0) + (0 if is_alert and e.key == "quota" else 1)
            item.update(titre="Pas réussi à analyser", categorie="Échec", note=0, a_retenir="",
                        resume=str(e)[:250], essais=tries)
            ko.append(item)
            if is_alert:
                failed.add(e.key)
                alert(e.key, e.alert_text)
            if tries >= MAX_TRIES:
                abandoned.append(item)
            print("ECHEC", item["lien"], e)
        if item in todo_new and item not in reels:
            reels.append(item)
        json.dump(reels, open(DATA, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        json.dump(state, open(STATE, "w"))
        git_save(f"réel {n}/{len(todo)} : {item['titre'][:50]}")
        time.sleep(5)  # petite pause entre deux réels pour ne pas saturer Gemini

    for key in worked - failed:
        resolve(key)  # le problème est réglé : l'alerte disparaît toute seule

    # ---- bilan final
    lines = [f"✅ Tri terminé : {len(ok)}/{len(todo)} réel(s) classé(s)"]
    par_cat = {}
    for r in ok:
        par_cat[r["categorie"]] = par_cat.get(r["categorie"], 0) + 1
    lines += [f"   • {c} : {nb}" for c, nb in sorted(par_cat.items(), key=lambda x: -x[1])]
    top = sorted(ok, key=lambda r: -(r.get("note") or 0))[:3]
    if top:
        lines.append("\n🏆 Les meilleurs :")
        lines += [f"   {'⭐' * int(r.get('note') or 0)} {r['titre']}" for r in top]
    if ko:
        lines.append(f"\n⚠️ {len(ko)} échec(s) :")
        for r in ko:
            again = "abandonné" if r["essais"] >= MAX_TRIES else "retenté au prochain tri"
            lines.append(f"   • {r['resume'][:90]} ({again})")
    lines.append(f"\n📊 {total()} réels classés au total")
    status("\n".join(lines))
    if abandoned:  # alerte : ceux-là ne seront plus retentés
        alert(f"abandon-{TODAY}", "🗑️ J'abandonne ces réels après 3 essais, regarde-les toi-même :\n"
              + "\n".join(f"• {r['lien']}" for r in abandoned))
    return reels


if __name__ == "__main__":
    state = json.load(open(STATE)) if os.path.exists(STATE) else {}
    try:
        main(state)
        state.get("alerts", {}).pop("crash", None)
    except Exception as e:  # le bot a planté : on prévient, avec la cause
        import traceback
        traceback.print_exc()
        chat = state.get("chat") or (int(ALLOWED) if ALLOWED else None)
        if chat:
            tg("sendMessage", chat_id=chat, text=f"💥 Le tri a planté : {str(e)[:300]}\n"
               "Envoie cette erreur à Claude pour qu'il corrige.")
        json.dump(state, open(STATE, "w"))
        git_save("sauvegarde après plantage")
        sys.exit(1)
    json.dump(state, open(STATE, "w"))
    git_save("tri terminé")
