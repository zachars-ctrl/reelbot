"""Reelbot : relève les liens envoyés au bot Telegram, fait analyser chaque vidéo par Gemini,
range le résultat dans reels.json (affiché par index.html) et supprime les messages traités."""
import datetime, glob, json, os, re, subprocess, tempfile, time
import requests
from google import genai
from google.genai import types

TG = f"https://api.telegram.org/bot{os.environ['TELEGRAM_TOKEN']}"
ALLOWED = os.environ.get("ALLOWED_CHAT_ID", "").strip()
MODELS = [m for m in [os.environ.get("GEMINI_MODEL", "").strip(), "gemini-3.8-flash", "gemini-3.5-flash", "gemini-3.1-flash-lite"] if m]
MAX_TRIES = 3  # un réel en échec est retenté aux tris suivants, 3 fois maximum
client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
URL_RE = re.compile(r"https?://\S+")
DATA = "reels.json"
CATEGORIES = ["Tuto IA", "Outil IA", "Actu IA", "Business", "Marrant", "Autre"]
owner, _, repo = os.environ.get("GITHUB_REPOSITORY", "/").partition("/")
PAGE_URL = f"https://{owner.lower()}.github.io/{repo}/"

PROMPT = """Tu analyses une courte vidéo sauvegardée par un jeune qui s'intéresse surtout
à l'IA, aux automatisations et aux nouveaux outils. Description d'origine : {desc}
Réponds UNIQUEMENT en JSON, en français, avec ces clés :
- "titre" : titre très bien choisi, précis et accrocheur, qui dit concrètement ce qu'on apprend (max 8 mots)
- "categorie" : EXACTEMENT une parmi {cats}.
   Tuto IA = montre comment faire quelque chose avec l'IA ; Outil IA = présente un outil/appli ;
   Actu IA = nouvelle, annonce ; Business = argent, entrepreneuriat ; Marrant = humour/divertissement.
   Si tu n'es pas sûr, mets "Autre".
- "note" : entier 1 à 5 (5 = très utile et concret, 1 = vide / pur buzz)
- "resume" : 1 à 2 phrases courtes sur ce que la vidéo apporte
- "a_retenir" : l'outil, l'astuce ou le lien clé à retenir (ou "" si rien)"""


def tg(method, **params):
    try:
        return requests.post(f"{TG}/{method}", json=params, timeout=60).json()
    except Exception as e:
        return {"ok": False, "error": str(e)}


def download(url, tmp):
    cmd = ["yt-dlp", "-q", "--no-playlist", "-f", "best[ext=mp4][height<=720]/best[ext=mp4]/best",
           "--max-filesize", "150M", "--write-info-json", "-o", f"{tmp}/v.%(ext)s", url]
    if os.path.exists("cookies.txt"):
        cmd[1:1] = ["--cookies", "cookies.txt"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    vids = [f for f in glob.glob(f"{tmp}/v.*") if not f.endswith(".json")]
    if not vids:
        raise RuntimeError("téléchargement impossible : " + (r.stderr.strip().splitlines() or ["?"])[-1][:200])
    desc = ""
    for j in glob.glob(f"{tmp}/*.info.json"):
        info = json.load(open(j))
        desc = (info.get("description") or info.get("title") or "")[:1500]
    return vids[0], desc


def analyse(url):
    is_yt = "youtube.com" in url or "youtu.be" in url
    with tempfile.TemporaryDirectory() as tmp:
        if is_yt:  # Gemini lit YouTube directement
            parts, desc = [types.Part(file_data=types.FileData(file_uri=url))], ""
        else:
            path, desc = download(url, tmp)
            f = client.files.upload(file=path)
            while f.state.name == "PROCESSING":
                time.sleep(3)
                f = client.files.get(name=f.name)
            parts = [f]
        last_err = None
        for model in MODELS:
            for attempt in range(3):
                try:
                    resp = client.models.generate_content(
                        model=model,
                        contents=parts + [PROMPT.format(desc=desc or "(aucune)", cats=", ".join(CATEGORIES))],
                        config=types.GenerateContentConfig(response_mime_type="application/json"))
                    d = json.loads(resp.text)
                    d = d[0] if isinstance(d, list) else d
                    if d.get("categorie") not in CATEGORIES:
                        d["categorie"] = "Autre"
                    return d
                except Exception as e:
                    last_err = e
                    msg = str(e)
                    print(f"  {model} essai {attempt + 1} : {msg[:150]}")
                    if any(k in msg for k in ("429", "RESOURCE_EXHAUSTED", "503", "UNAVAILABLE", "500")):
                        time.sleep(30 * (attempt + 1))  # trop de demandes d'un coup : on patiente
                        continue
                    break  # modèle indisponible : on passe au suivant
        m = str(last_err)
        if "429" in m or "RESOURCE_EXHAUSTED" in m:
            raise RuntimeError("Quota Gemini atteint, nouvel essai au prochain tri.")
        raise RuntimeError(f"Gemini : {m[:200]}")


STATE = "etat.json"  # garde l'id du dernier message de statut, pour l'effacer au tri suivant


def main():
    # Bouton "📂 Réels" en bas de la conversation, qui ouvre la page
    if owner and repo:
        tg("setChatMenuButton", menu_button={"type": "web_app", "text": "📂 Réels", "web_app": {"url": PAGE_URL}})

    updates = tg("getUpdates", timeout=0).get("result", [])
    reels = json.load(open(DATA, encoding="utf-8")) if os.path.exists(DATA) else []
    state = json.load(open(STATE)) if os.path.exists(STATE) else {}
    known = {r["lien"] for r in reels}

    # 1) On fait la liste de ce qu'il y a à faire
    chat = int(ALLOWED) if ALLOWED else state.get("chat")
    todo_new, to_delete = [], []
    for u in updates:
        msg = u.get("message") or {}
        c = msg.get("chat", {}).get("id")
        if not c or (ALLOWED and str(c) != ALLOWED):
            continue
        chat = c
        to_delete.append((c, msg.get("message_id")))
        text = (msg.get("text") or "") + " " + (msg.get("caption") or "")
        for url in URL_RE.findall(text):
            if url not in known:
                known.add(url)
                todo_new.append({"date": datetime.date.today().isoformat(), "lien": url})
    todo_retry = [r for r in reels if r.get("categorie") == "Échec" and r.get("essais", 1) < MAX_TRIES]
    todo = todo_retry + todo_new

    # 2) On nettoie la conversation : messages envoyés + ancien statut
    for c, mid in to_delete:
        tg("deleteMessage", chat_id=c, message_id=mid)
    if chat and state.get("status_id"):
        tg("deleteMessage", chat_id=chat, message_id=state["status_id"])
    if updates:
        tg("getUpdates", offset=updates[-1]["update_id"] + 1, timeout=0)  # marque comme traités

    if not chat:
        print("Aucune conversation connue.")
        return
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

    state["status_id"] = None
    if not todo:
        status("✅ Rien à trier : aucun nouveau réel et aucun échec à retenter.\n"
               f"📊 {len([r for r in reels if r.get('categorie') != 'Échec'])} réels classés au total.")
    else:
        status(f"⏳ Tri en cours : {len(todo)} réel(s)\n"
               f"• {len(todo_new)} nouveau(x)\n• {len(todo_retry)} échec(s) à retenter")

    # 3) On analyse, en affichant l'avancement
    ok, ko = [], []
    for n, item in enumerate(todo, 1):
        try:
            d = analyse(item["lien"])
            for k in ["titre", "categorie", "note", "resume", "a_retenir"]:
                item[k] = d.get(k, "")
            item.pop("essais", None)
            ok.append(item)
            print("OK", item["lien"], item["titre"])
        except Exception as e:
            item.update(titre="Pas réussi à analyser", categorie="Échec", note=0, a_retenir="",
                        resume=str(e)[:250], essais=item.get("essais", 0) + 1)
            ko.append(item)
            print("ECHEC", item["lien"], e)
        if item in todo_new:
            reels.append(item)
        status(f"⏳ Tri en cours : {n}/{len(todo)}\n✅ {len(ok)} classé(s)   ⚠️ {len(ko)} échec(s)\n"
               f"Dernier : {item['titre']}")
        json.dump(reels, open(DATA, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        time.sleep(5)  # petite pause entre deux réels pour ne pas saturer Gemini

    # 4) Bilan final
    if todo:
        lines = [f"✅ Tri terminé : {len(ok)}/{len(todo)} réel(s) classé(s)"]
        par_cat = {}
        for r in ok:
            par_cat[r["categorie"]] = par_cat.get(r["categorie"], 0) + 1
        for cat, nb in sorted(par_cat.items(), key=lambda x: -x[1]):
            lines.append(f"   • {cat} : {nb}")
        top = sorted(ok, key=lambda r: -(r.get("note") or 0))[:3]
        if top:
            lines.append("\n🏆 Les meilleurs :")
            lines += [f"   {'⭐' * int(r.get('note') or 0)} {r['titre']}" for r in top]
        if ko:
            lines.append(f"\n⚠️ {len(ko)} échec(s) :")
            for r in ko:
                again = "retenté au prochain tri" if r["essais"] < MAX_TRIES else "abandonné"
                lines.append(f"   • {r['resume'][:90]} ({again})")
        total = len([r for r in reels if r.get("categorie") != "Échec"])
        lines.append(f"\n📊 {total} réels classés au total")
        status("\n".join(lines))

    json.dump(reels, open(DATA, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    state["chat"] = chat
    json.dump(state, open(STATE, "w"))
    subprocess.run(["git", "add", STATE, DATA], capture_output=True)  # pour que GitHub les sauvegarde


if __name__ == "__main__":
    main()
