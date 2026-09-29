"""Reelbot : relève les liens envoyés au bot Telegram, fait analyser chaque vidéo par Gemini,
range le résultat dans reels.json (affiché par index.html) et supprime les messages traités."""
import datetime, glob, json, os, re, subprocess, tempfile, time
import requests
from google import genai
from google.genai import types

TG = f"https://api.telegram.org/bot{os.environ['TELEGRAM_TOKEN']}"
ALLOWED = os.environ.get("ALLOWED_CHAT_ID", "").strip()
MODELS = [m for m in [os.environ.get("GEMINI_MODEL", "").strip(), "gemini-3.8-flash", "gemini-2.5-flash"] if m]
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
        raise RuntimeError(f"Gemini : {last_err}")


def main():
    # Bouton "📂 Réels" en bas de la conversation, qui ouvre la page
    if owner and repo:
        tg("setChatMenuButton", menu_button={"type": "web_app", "text": "📂 Réels", "web_app": {"url": PAGE_URL}})

    updates = tg("getUpdates", timeout=0).get("result", [])
    if not updates:
        print("Rien de nouveau.")
        return
    reels = json.load(open(DATA, encoding="utf-8")) if os.path.exists(DATA) else []
    known = {r["lien"] for r in reels}

    for u in updates:
        msg = u.get("message") or {}
        chat, mid = msg.get("chat", {}).get("id"), msg.get("message_id")
        if not chat or (ALLOWED and str(chat) != ALLOWED):
            continue
        text = (msg.get("text") or "") + " " + (msg.get("caption") or "")
        for url in URL_RE.findall(text):
            if url in known:
                continue
            item = {"date": datetime.date.today().isoformat(), "lien": url}
            try:
                d = analyse(url)
                item.update({k: d.get(k, "") for k in ["titre", "categorie", "note", "resume", "a_retenir"]})
                print("OK", url, item["titre"])
            except Exception as e:
                item.update(titre="Pas réussi à analyser", categorie="Échec", note=0, resume=str(e)[:300], a_retenir="")
                print("ECHEC", url, e)
            reels.append(item)
            known.add(url)
        tg("deleteMessage", chat_id=chat, message_id=mid)  # garde la conversation propre

    json.dump(reels, open(DATA, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    tg("getUpdates", offset=updates[-1]["update_id"] + 1, timeout=0)  # marque comme traités


if __name__ == "__main__":
    main()
