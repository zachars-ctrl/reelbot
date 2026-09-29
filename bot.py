"""Reelbot : trie les vidéos (Instagram, TikTok, YouTube, posts photo) envoyées au bot Telegram.

Modes (variable MODE) :
  tri        (défaut) analyse les nouveaux liens + retente les échecs
  reclasser  reclasse toutes les vidéos d'après leur résumé (rapide, sans retélécharger)

Fichiers du dépôt :
  reels.json          les vidéos analysées (écrit par le bot)
  categories_ia.json  les catégories créées par l'IA (écrit par le bot)
  categories.json     TES réglages de catégories : noms, icônes, couleurs, fusions (écrit par la page)
  perso.json          TES priorités, « vu », Ma liste, déplacements (écrit par la page)
  thumbs/             les miniatures
  inbox/              les liens déposés par le relais Cloudflare
  etat.json           mémoire du bot (message de statut, alertes)
"""
import datetime, glob, hashlib, json, mimetypes, os, re, subprocess, sys, tempfile, time, unicodedata
import requests
from google import genai
from google.genai import types

TG = f"https://api.telegram.org/bot{os.environ['TELEGRAM_TOKEN']}"
ALLOWED = os.environ.get("ALLOWED_CHAT_ID", "").strip()
MODE = (os.environ.get("MODE") or "tri").strip()
VIDEO_MODELS = [m for m in [os.environ.get("GEMINI_MODEL", "").strip(),
                            "gemini-3.8-flash", "gemini-3.5-flash", "gemini-3.1-flash-lite"] if m]
TEXT_MODELS = ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-3.8-flash"]
MAX_TRIES = 3
TIME_BUDGET = 20 * 60
client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
URL_RE = re.compile(r"https?://\S+")
DATA, STATE, INBOX, THUMBS = "reels.json", "etat.json", "inbox", "thumbs"
CATS_IA, CATS_USER, PERSO = "categories_ia.json", "categories.json", "perso.json"
owner, _, repo = os.environ.get("GITHUB_REPOSITORY", "/").partition("/")
PAGE_URL = f"https://{owner.lower()}.github.io/{repo}/"
TODAY = datetime.date.today().isoformat()

ICONES = ["smart_toy", "settings_suggest", "build", "school", "newspaper", "work", "payments", "trending_up",
          "skillet", "restaurant", "local_cafe", "fitness_center", "directions_run", "self_improvement",
          "sentiment_very_satisfied", "theater_comedy", "flight", "travel_explore", "home", "chair",
          "checkroom", "brush", "palette", "photo_camera", "movie", "music_note", "sports_esports",
          "psychology", "favorite", "pets", "eco", "science", "code", "smartphone", "directions_car",
          "shopping_bag", "lightbulb", "menu_book", "public", "folder"]
COULEURS = ["blue", "orange", "green", "pink", "purple", "teal", "yellow", "red", "indigo", "mint", "brown", "gray"]
DEFAULT_ICONS = {"tuto-ia": "school", "outil-ia": "build", "actu-ia": "newspaper", "business": "work",
                 "marrant": "sentiment_very_satisfied", "humour": "sentiment_very_satisfied", "autre": "folder",
                 "cuisine": "skillet", "sport": "fitness_center", "automatisation": "settings_suggest"}


def slug(s):
    s = unicodedata.normalize("NFD", str(s or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-") or "autre"


def load(path, default):
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return default


def dump(path, data):
    json.dump(data, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def item_id(url):
    return hashlib.sha1(url.encode()).hexdigest()[:10]


def source_of(url):
    for k, v in (("instagram.com", "Instagram"), ("tiktok.com", "TikTok"), ("youtube.com", "YouTube"),
                 ("youtu.be", "YouTube"), ("x.com", "X"), ("twitter.com", "X"), ("facebook.com", "Facebook")):
        if k in url:
            return v
    return "Web"


# ---------------------------------------------------------------- Catégories
def categories():
    """Catégories finales = celles de l'IA, corrigées par tes réglages (noms, icônes, suppressions, fusions)."""
    ia = load(CATS_IA, [])
    user = load(CATS_USER, {"categories": [], "alias": {}})
    cats = {c["id"]: dict(c) for c in ia}
    for c in user.get("categories", []):
        cats[c["id"]] = {**cats.get(c["id"], {}), **c}
    alias = user.get("alias", {})
    live = {k: v for k, v in cats.items() if not v.get("supprime") and k not in alias and k != "autre"}
    deleted = {k for k, v in cats.items() if v.get("supprime")} | set(alias)
    return live, alias, deleted


def seed_categories(reels):
    """Première fois : crée categories_ia.json à partir des catégories déjà utilisées."""
    if os.path.exists(CATS_IA):
        return
    ia, used = [], []
    for r in reels:
        c = r.get("categorie")
        if c and c not in ("Échec", "Autre") and c not in used:
            used.append(c)
    for i, name in enumerate(used):
        ia.append({"id": slug(name), "nom": name, "icone": DEFAULT_ICONS.get(slug(name), "folder"),
                   "couleur": COULEURS[i % len(COULEURS)], "description": ""})
    dump(CATS_IA, ia)


def add_ia_category(new, deleted):
    """L'IA propose une nouvelle catégorie : on l'ajoute (sauf si tu l'as supprimée)."""
    ia = load(CATS_IA, [])
    cid = slug(new.get("nom"))
    if cid in deleted or cid == "autre":
        return None
    if not any(c["id"] == cid for c in ia):
        used = {c.get("couleur") for c in ia}
        color = next((c for c in COULEURS if c not in used), COULEURS[len(ia) % len(COULEURS)])
        icon = new.get("icone") if new.get("icone") in ICONES else "folder"
        ia.append({"id": cid, "nom": str(new.get("nom"))[:40], "icone": icon, "couleur": color,
                   "description": str(new.get("description", ""))[:300], "cree_le": TODAY})
        dump(CATS_IA, ia)
    return cid


def cats_prompt(live):
    lines = [f'- "{c["nom"]}" : {c.get("description") or "(pas de description)"}' for c in live.values()]
    return "\n".join(lines) or "(aucune pour l'instant)"


def resolve(ans, live, alias, deleted):
    """Transforme la réponse de l'IA en (id, nom) de catégorie."""
    name = str(ans.get("categorie") or "").strip()
    by_name = {slug(c["nom"]): k for k, c in live.items()}
    cid = by_name.get(slug(name)) or (slug(name) if slug(name) in live else None)
    if not cid and isinstance(ans.get("nouvelle_categorie"), dict) and ans["nouvelle_categorie"].get("nom"):
        cid = add_ia_category(ans["nouvelle_categorie"], deleted)
        if cid:
            live[cid] = {"id": cid, "nom": ans["nouvelle_categorie"]["nom"]}
    if not cid and name and slug(name) not in deleted and slug(name) != "autre":
        cid = add_ia_category({"nom": name}, deleted)
        if cid:
            live[cid] = {"id": cid, "nom": name}
    while cid in alias:
        cid = alias[cid]
    if not cid or cid not in live:
        return "autre", "Autre"
    return cid, live[cid]["nom"]


PROMPT = """Tu analyses un contenu court (vidéo, ou une ou plusieurs images d'un carrousel) qu'un jeune de 18 ans
a enregistré pour le regarder plus tard. Ça peut parler de TOUT : cuisine, sport, IA, business, humour, voyage, tech, mode…
S'il y a du texte dans les images ou à l'écran, lis-le : c'est souvent là qu'est l'information.
Description d'origine : {desc}

Catégories existantes (nom : ce qui va dedans) :
{cats}

Réponds UNIQUEMENT en JSON, en français, avec ces clés :
- "titre" : titre court et factuel, max 7 mots, qui dit ce qu'on y trouve. Pas de pièges à clic, pas d'emoji,
   pas de majuscules partout. Exemples : "Trier ses mails avec n8n", "Carbonara sans crème", "Épaules sans matériel".
- "categorie" : le NOM EXACT d'une catégorie existante qui convient.
- "nouvelle_categorie" : null, SAUF si aucune catégorie existante ne convient vraiment ET que le sujet est assez large
   pour revenir souvent. Alors : {{"nom": "1 ou 2 mots, large (ex. Cuisine, Voyage, Mode)", "icone": un nom parmi {icones},
   "description": "ce qui ira dans cette catégorie"}} et mets le même nom dans "categorie".
- "verdict" : ton avis cash en une phrase de 3 à 8 mots, familier, sans filtre, comme un pote honnête.
   Exemples : "Pépite, à tester ce soir.", "Du vent, zappe.", "Sympa mais rien de neuf.", "Utile si tu pars à Lisbonne."
- "resume" : 1 à 2 phrases courtes sur ce que le contenu apporte concrètement.
- "a_retenir" : l'outil, l'astuce, la recette ou le lien clé à retenir (ou "" si rien).
- "note" : entier 1 à 5 (5 = très utile ou excellent dans son genre, 1 = vide)."""

RECLASS_PROMPT = """Tu ranges une bibliothèque de vidéos courtes dans des catégories.
Catégories existantes (nom : ce qui va dedans) :
{cats}

Pour CHAQUE vidéo ci-dessous, choisis le NOM EXACT de la catégorie la plus logique.
Si vraiment aucune ne convient et que le sujet est large, tu peux inventer une nouvelle catégorie (1 ou 2 mots,
large) : mets son nom dans "categorie" et ajoute "nouvelle_categorie": {{"nom", "icone" parmi {icones}, "description"}}.
Si la vidéo n'a pas de "verdict", écris-en un : avis cash, 3 à 8 mots, familier, comme un pote honnête.
Réponds UNIQUEMENT par une liste JSON : [{{"i": numéro, "categorie": "…", "verdict": "…" (seulement si absent),
"nouvelle_categorie": null ou {{…}}}}]

Vidéos :
{items}"""


# ---------------------------------------------------------------- Telegram
def tg(method, **params):
    try:
        return requests.post(f"{TG}/{method}", json=params, timeout=60).json()
    except Exception as e:
        return {"ok": False, "description": str(e)}


class Alert(RuntimeError):
    def __init__(self, key, short, alert_text):
        super().__init__(short)
        self.key, self.alert_text = key, alert_text


INSTA_BLOCK = ("login", "log in", "rate-limit", "rate limit", "not available", "401", "403",
               "429", "cookies", "checkpoint", "please wait")
ALERT_INSTA = ("🔒 Instagram me bloque (il demande une connexion).\n"
               "À faire une fois : ajoute tes cookies Instagram dans le secret GitHub INSTAGRAM_COOKIES. "
               "Demande à Claude « ajoute les cookies Insta au reelbot » pour le pas-à-pas.\n"
               "En attendant, les vidéos bloquées sont gardées et retentées.")
ALERT_QUOTA = ("⛽ Quota Gemini gratuit épuisé pour aujourd'hui.\n"
               "Rien à faire : les vidéos restantes sont gardées et je reprends au prochain tri.")
ALERT_KEY = ("🔑 Google refuse la clé Gemini (clé invalide ou supprimée).\n"
             "À faire : crée une nouvelle clé sur aistudio.google.com, puis remplace le secret GitHub GEMINI_API_KEY.")


# ---------------------------------------------------------------- Téléchargement + miniature
def run(cmd, timeout=300):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def with_cookies(cmd):
    if os.path.exists("cookies.txt"):
        cmd[1:1] = ["--cookies", "cookies.txt"]
    return cmd


def make_thumb(src, iid):
    """Miniature carrée 360 px dans thumbs/ (légère, pour la page)."""
    try:
        from PIL import Image
        os.makedirs(THUMBS, exist_ok=True)
        im = Image.open(src).convert("RGB")
        w, h = im.size
        s = min(w, h)
        im = im.crop(((w - s) // 2, (h - s) // 2, (w - s) // 2 + s, (h - s) // 2 + s)).resize((360, 360))
        out = f"{THUMBS}/{iid}.jpg"
        im.save(out, "JPEG", quality=72, optimize=True)
        return out
    except Exception as e:
        print("  miniature impossible :", e)
        return ""


def video_frame(video, tmp):
    out = f"{tmp}/frame.jpg"
    run(["ffmpeg", "-y", "-loglevel", "error", "-ss", "1", "-i", video, "-frames:v", "1", out], 60)
    return out if os.path.exists(out) else ""


def download(url, tmp):
    """Renvoie (fichiers médias, description, durée en s, image pour la miniature)."""
    errors = []
    r = run(with_cookies(["yt-dlp", "-q", "--no-playlist", "-f", "best[ext=mp4][height<=720]/best[ext=mp4]/best",
                          "--max-filesize", "150M", "--write-info-json", "--write-thumbnail",
                          "-o", f"{tmp}/v.%(ext)s", "-o", f"thumbnail:{tmp}/thumb.%(ext)s", url]))
    vids = [f for f in glob.glob(f"{tmp}/v.*") if (mimetypes.guess_type(f)[0] or "").startswith("video")]
    if vids:
        desc, dur = "", None
        for j in glob.glob(f"{tmp}/*.info.json"):
            info = json.load(open(j))
            desc = (info.get("description") or info.get("title") or "")[:1500]
            dur = info.get("duration")
        thumbs = glob.glob(f"{tmp}/thumb.*")
        return vids[:1], desc, dur, (thumbs[0] if thumbs else video_frame(vids[0], tmp))
    errors.append(r.stderr.strip())

    gdir = f"{tmp}/g"
    r = run(with_cookies(["gallery-dl", "-q", "--write-metadata", "-D", gdir, url]))
    files = sorted(f for f in glob.glob(f"{gdir}/*")
                   if (mimetypes.guess_type(f)[0] or "").split("/")[0] in ("image", "video"))
    if files:
        desc = ""
        for j in sorted(glob.glob(f"{gdir}/*.json")):
            meta = json.load(open(j))
            desc = (meta.get("description") or meta.get("caption") or "")[:1500]
            if desc:
                break
        first_img = next((f for f in files if (mimetypes.guess_type(f)[0] or "").startswith("image")), "")
        if not first_img:
            first_img = video_frame(files[0], tmp)
        return files[:10], desc, None, first_img
    errors.append(r.stderr.strip() or r.stdout.strip())

    msg = " | ".join(e.splitlines()[-1] for e in errors if e)[:300] or "aucun média trouvé"
    if "instagram.com" in url and any(k in msg.lower() for k in INSTA_BLOCK):
        raise Alert("insta", "Instagram demande une connexion (cookies).", ALERT_INSTA)
    raise RuntimeError("téléchargement impossible : " + msg)


def youtube_id(url):
    m = re.search(r"(?:v=|youtu\.be/|shorts/|embed/)([A-Za-z0-9_-]{11})", url)
    return m.group(1) if m else None


# ---------------------------------------------------------------- Gemini
def upload(path):
    f = client.files.upload(file=path)
    while f.state.name == "PROCESSING":
        time.sleep(3)
        f = client.files.get(name=f.name)
    return f


def ask_gemini(models, contents, on_wait=lambda t: None):
    errs = []
    for model in models:
        for attempt in range(3):
            try:
                resp = client.models.generate_content(
                    model=model, contents=contents,
                    config=types.GenerateContentConfig(response_mime_type="application/json"))
                return json.loads(resp.text)
            except Exception as e:
                msg = str(e)
                errs.append(msg)
                print(f"  {model} essai {attempt + 1} : {msg[:200]}")
                if "API_KEY_INVALID" in msg or "API key not valid" in msg or "PERMISSION_DENIED" in msg:
                    raise Alert("cle", "Clé Gemini refusée.", ALERT_KEY)
                if "PerDay" in msg or "per day" in msg.lower():
                    break
                if any(k in msg for k in ("429", "RESOURCE_EXHAUSTED", "503", "UNAVAILABLE", "500")) and attempt < 2:
                    wait = 30 * (attempt + 1)
                    on_wait(f"Gemini saturé, nouvel essai dans {wait} s…")
                    time.sleep(wait)
                    continue
                break
    if errs and all(("429" in e or "RESOURCE_EXHAUSTED" in e) for e in errs):
        raise Alert("quota", "Quota Gemini épuisé, repris au prochain tri.", ALERT_QUOTA)
    raise RuntimeError(f"Gemini : {errs[-1][:200] if errs else '?'}")


def analyse(item, live, alias, deleted, on_wait=lambda t: None):
    url = item["lien"]
    with tempfile.TemporaryDirectory() as tmp:
        yid = youtube_id(url)
        if yid:  # Gemini lit YouTube directement
            parts, desc, dur, thumb = [types.Part(file_data=types.FileData(file_uri=url))], "", None, None
            item["miniature"] = f"https://i.ytimg.com/vi/{yid}/hqdefault.jpg"
            meta = run(["yt-dlp", "-j", "--skip-download", url], 60)
            try:
                dur = json.loads(meta.stdout).get("duration")
            except Exception:
                pass
        else:
            files, desc, dur, thumb = download(url, tmp)
            parts = [upload(p) for p in files]
            if thumb:
                item["miniature"] = make_thumb(thumb, item["id"]) or item.get("miniature", "")
        if dur:
            item["duree"] = int(dur)
        prompt = PROMPT.format(desc=desc or "(aucune)", cats=cats_prompt(live), icones=", ".join(ICONES))
        d = ask_gemini(VIDEO_MODELS, parts + [prompt], on_wait)
    d = d[0] if isinstance(d, list) else d
    cid, cname = resolve(d, live, alias, deleted)
    try:
        note = max(1, min(5, int(d.get("note") or 3)))
    except Exception:
        note = 3
    item.update(titre=str(d.get("titre", ""))[:80], cat=cid, categorie=cname, note=note,
                verdict=str(d.get("verdict", ""))[:90], resume=str(d.get("resume", ""))[:300],
                a_retenir=str(d.get("a_retenir", ""))[:200])
    item.pop("essais", None)
    return item


# ---------------------------------------------------------------- Sauvegarde GitHub
def git_save(msg):
    if not os.environ.get("GITHUB_ACTIONS"):
        return
    g = ["git", "-c", "user.name=reelbot", "-c", "user.email=reelbot@users.noreply.github.com"]
    paths = [p for p in (DATA, STATE, CATS_IA, INBOX, THUMBS) if os.path.exists(p)]
    subprocess.run(g + ["add", "-A"] + paths, capture_output=True)
    subprocess.run(g + ["add", "-u"], capture_output=True)
    if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode == 0:
        return
    subprocess.run(g + ["commit", "-q", "-m", msg], capture_output=True)
    for _ in range(4):
        subprocess.run(g + ["pull", "-q", "--rebase", "-X", "theirs"], capture_output=True)
        if subprocess.run(["git", "push", "-q"], capture_output=True).returncode == 0:
            return
        time.sleep(3)


def migrate(reels):
    """Anciennes vidéos : ajoute id, source et identifiant de catégorie."""
    for r in reels:
        r.setdefault("id", item_id(r["lien"]))
        r.setdefault("source", source_of(r["lien"]))
        if "cat" not in r and r.get("categorie") not in (None, "", "Échec"):
            r["cat"] = slug(r["categorie"])


# ---------------------------------------------------------------- Statut + alertes
class Chat:
    def __init__(self, state, chat):
        self.state, self.chat = state, chat
        self.alerts = state.setdefault("alerts", {})
        self.button = {"inline_keyboard": [[{"text": "📂 Ouvrir mes vidéos", "web_app": {"url": PAGE_URL}}]]} if owner else None

    def status(self, text):
        params = dict(chat_id=self.chat, text=text, disable_web_page_preview=True)
        if self.button:
            params["reply_markup"] = self.button
        if self.state.get("status_id"):
            r = tg("editMessageText", message_id=self.state["status_id"], **params)
            if r.get("ok") or "not modified" in str(r.get("description", "")):
                return
        r = tg("sendMessage", **params)
        if r.get("ok"):
            self.state["status_id"] = r["result"]["message_id"]

    def alert(self, key, text):
        a = self.alerts.get(key)
        if a and a.get("date") == TODAY:
            return
        if a and a.get("mid"):
            tg("deleteMessage", chat_id=self.chat, message_id=a["mid"])
        r = tg("sendMessage", chat_id=self.chat, text=text)
        self.alerts[key] = {"date": TODAY, "mid": r.get("result", {}).get("message_id")}

    def resolve(self, key):
        a = self.alerts.pop(key, None)
        if a and a.get("mid"):
            tg("deleteMessage", chat_id=self.chat, message_id=a["mid"])


# ---------------------------------------------------------------- Mode tri
def mode_tri(state, reels, chat_id):
    known = {r["lien"] for r in reels}
    todo_new, to_delete, inbox_files = [], [], sorted(
        glob.glob(f"{INBOX}/*.json"), key=lambda f: int(re.sub(r"\D", "", os.path.basename(f)) or 0))
    chat = chat_id

    def take(c, mid, text):
        nonlocal chat
        if not c or (ALLOWED and str(c) != ALLOWED):
            return
        chat = c
        to_delete.append((c, mid))
        for url in URL_RE.findall(text or ""):
            url = url.rstrip(").,;!?")
            if url not in known:
                known.add(url)
                todo_new.append({"id": item_id(url), "date": TODAY, "lien": url, "source": source_of(url)})

    for f in inbox_files:
        m = load(f, {})
        take(m.get("chat"), m.get("message_id"), m.get("text"))
    updates = tg("getUpdates", timeout=0).get("result", [])
    for u in updates:
        msg = u.get("message") or {}
        take(msg.get("chat", {}).get("id"), msg.get("message_id"),
             (msg.get("text") or "") + " " + (msg.get("caption") or ""))

    todo_retry = [r for r in reels if r.get("categorie") == "Échec" and r.get("essais", 1) < MAX_TRIES]
    todo = todo_retry + todo_new

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
    ui = Chat(state, chat)
    live, alias, deleted = categories()
    total = lambda: len([r for r in reels if r.get("categorie") != "Échec"])

    if not todo:
        ui.status(f"✅ Rien à trier : aucune nouvelle vidéo et aucun échec à retenter.\n📊 {total()} vidéos rangées au total.")
        return
    ui.status(f"⏳ Tri en cours : {len(todo)} vidéo(s)\n• {len(todo_new)} nouvelle(s)\n• {len(todo_retry)} échec(s) à retenter")

    ok, ko, abandoned, worked, failed, start = [], [], [], set(), set(), time.time()
    new_cats_before = set(live)
    for n, item in enumerate(todo, 1):
        if time.time() - start > TIME_BUDGET:
            for rest in todo[n - 1:]:
                if rest in todo_new:
                    rest.update(titre="En attente", categorie="Échec", note=0, a_retenir="", verdict="", essais=0,
                                resume="Pas eu le temps, repris au prochain tri.")
                    reels.append(rest)
            ui.status(f"⏸️ Tri arrêté après 20 min : {len(ok)} rangée(s), le reste sera fait au prochain tri.")
            todo = todo[:n - 1]
            break
        head = lambda: f"⏳ Tri en cours : vidéo {n}/{len(todo)}\n✅ {len(ok)} rangée(s)   ⚠️ {len(ko)} échec(s)\n"
        ui.status(head() + "🔎 Analyse en cours…")
        try:
            analyse(item, live, alias, deleted, on_wait=lambda t: ui.status(head() + "⌛ " + t))
            ok.append(item)
            worked.update({"cle", "quota"} | ({"insta"} if "instagram.com" in item["lien"] else set()))
            print("OK", item["lien"], item["titre"], "->", item["categorie"])
        except Exception as e:
            is_alert = isinstance(e, Alert)
            tries = item.get("essais", 0) + (0 if is_alert and e.key == "quota" else 1)
            item.update(titre="Pas réussi à analyser", categorie="Échec", note=0, a_retenir="", verdict="",
                        resume=str(e)[:250], essais=tries)
            item.pop("cat", None)
            ko.append(item)
            if is_alert:
                failed.add(e.key)
                ui.alert(e.key, e.alert_text)
            if tries >= MAX_TRIES:
                abandoned.append(item)
            print("ECHEC", item["lien"], e)
        if item in todo_new and item not in reels:
            reels.append(item)
        dump(DATA, reels)
        dump(STATE, state)
        git_save(f"vidéo {n}/{len(todo)} : {item['titre'][:50]}")
        time.sleep(5)

    for key in worked - failed:
        ui.resolve(key)

    lines = [f"✅ Tri terminé : {len(ok)}/{len(todo)} vidéo(s) rangée(s)"]
    par_cat = {}
    for r in ok:
        par_cat[r["categorie"]] = par_cat.get(r["categorie"], 0) + 1
    lines += [f"   • {c} : {nb}" for c, nb in sorted(par_cat.items(), key=lambda x: -x[1])]
    created = [live[c]["nom"] for c in set(live) - new_cats_before]
    if created:
        lines.append(f"\n🆕 Nouvelle(s) catégorie(s) : {', '.join(created)}")
    top = sorted(ok, key=lambda r: -(r.get("note") or 0))[:3]
    if top:
        lines.append("\n🏆 Les meilleures :")
        lines += [f"   • {r['titre']} — {r.get('verdict', '')}" for r in top]
    if ko:
        lines.append(f"\n⚠️ {len(ko)} échec(s) :")
        for r in ko:
            again = "abandonné" if r["essais"] >= MAX_TRIES else "retenté au prochain tri"
            lines.append(f"   • {r['resume'][:90]} ({again})")
    lines.append(f"\n📊 {total()} vidéos rangées au total")
    ui.status("\n".join(lines))
    if abandoned:
        ui.alert(f"abandon-{TODAY}", "🗑️ J'abandonne ces vidéos après 3 essais, regarde-les toi-même :\n"
                 + "\n".join(f"• {r['lien']}" for r in abandoned))


# ---------------------------------------------------------------- Mode reclasser
def mode_reclasser(state, reels, chat_id):
    chat = chat_id or state.get("chat")
    if not chat:
        return
    state["chat"] = chat
    if state.get("status_id"):
        tg("deleteMessage", chat_id=chat, message_id=state["status_id"])
    state["status_id"] = None
    ui = Chat(state, chat)
    live, alias, deleted = categories()
    items = [r for r in reels if r.get("categorie") != "Échec"]
    ui.status(f"🗂️ Reclassement de {len(items)} vidéo(s)…")
    changed, before = 0, set(live)
    for start in range(0, len(items), 25):
        batch = items[start:start + 25]
        listing = "\n".join(json.dumps({"i": i, "titre": r.get("titre"), "resume": r.get("resume"),
                                        "a_retenir": r.get("a_retenir"), "verdict": r.get("verdict") or None},
                                       ensure_ascii=False) for i, r in enumerate(batch))
        prompt = RECLASS_PROMPT.format(cats=cats_prompt(live), icones=", ".join(ICONES), items=listing)
        try:
            res = ask_gemini(TEXT_MODELS, [prompt])
        except Alert as e:
            ui.alert(e.key, e.alert_text)
            break
        except Exception as e:
            print("reclassement :", e)
            continue
        for a in res if isinstance(res, list) else []:
            try:
                r = batch[int(a.get("i"))]
            except Exception:
                continue
            cid, cname = resolve(a, live, alias, deleted)
            if r.get("cat") != cid:
                changed += 1
            r["cat"], r["categorie"] = cid, cname
            if a.get("verdict") and not r.get("verdict"):
                r["verdict"] = str(a["verdict"])[:90]
        dump(DATA, reels)
        git_save(f"reclassement {min(start + 25, len(items))}/{len(items)}")
        ui.status(f"🗂️ Reclassement : {min(start + 25, len(items))}/{len(items)}…")
    created = [live[c]["nom"] for c in set(live) - before]
    txt = f"✅ Reclassement terminé : {changed} vidéo(s) ont changé de catégorie."
    if created:
        txt += f"\n🆕 Nouvelle(s) catégorie(s) : {', '.join(created)}"
    txt += "\nTes déplacements manuels sont conservés."
    ui.status(txt)


# ---------------------------------------------------------------- Entrée
def main(state):
    if owner and repo:
        tg("setChatMenuButton", menu_button={"type": "web_app", "text": "📂 Vidéos", "web_app": {"url": PAGE_URL}})
    reels = load(DATA, [])
    migrate(reels)
    seed_categories(reels)
    chat_id = int(ALLOWED) if ALLOWED else (int(os.environ["CHAT_ID"]) if os.environ.get("CHAT_ID") else state.get("chat"))
    if MODE == "reclasser":
        mode_reclasser(state, reels, chat_id)
    else:
        mode_tri(state, reels, chat_id)
    dump(DATA, reels)


if __name__ == "__main__":
    state = load(STATE, {})
    try:
        main(state)
    except Exception as e:
        import traceback
        traceback.print_exc()
        chat = state.get("chat") or (int(ALLOWED) if ALLOWED else None)
        if chat:
            tg("sendMessage", chat_id=chat, text=f"💥 Le tri a planté : {str(e)[:300]}\n"
               "Envoie cette erreur à Claude pour qu'il corrige.")
        dump(STATE, state)
        git_save("sauvegarde après plantage")
        sys.exit(1)
    dump(STATE, state)
    git_save("tri terminé")
