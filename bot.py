"""Reelbot : lit les liens envoyés au bot Telegram, fait analyser chaque vidéo par Gemini,
répond sur Telegram et range tout dans reels.csv."""
import csv, datetime, glob, json, os, re, subprocess, tempfile, time
import requests
from google import genai
from google.genai import types

TG = f"https://api.telegram.org/bot{os.environ['TELEGRAM_TOKEN']}"
ALLOWED = os.environ.get("ALLOWED_CHAT_ID", "").strip()
MODELS = [m for m in [os.environ.get("GEMINI_MODEL", "").strip(), "gemini-3.8-flash", "gemini-2.5-flash"] if m]
client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
URL_RE = re.compile(r"https?://\S+")
CSV_FILE = "reels.csv"
FIELDS = ["date", "lien", "titre", "categorie", "note", "resume", "pourquoi", "a_retenir"]

PROMPT = """Tu analyses une courte vidéo sauvegardée par un jeune qui s'intéresse surtout
à l'IA, aux automatisations et aux nouveaux outils. Description d'origine : {desc}
Réponds UNIQUEMENT en JSON, en français, avec ces clés :
- "titre" : titre court et clair (max 8 mots)
- "categorie" : une parmi "Automatisation", "Nouvel outil IA", "Actu IA", "Tuto", "Business", "Divertissement", "Autre"
- "note" : entier 1 à 5 (5 = très utile et concret, 1 = vide / pur buzz)
- "resume" : 2 phrases max sur ce qui est montré
- "pourquoi" : 1 phrase qui justifie la note
- "a_retenir" : l'outil, l'astuce ou le lien clé à retenir (ou "" si rien)"""


def tg(method, **params):
    return requests.post(f"{TG}/{method}", json=params, timeout=60).json()


def download(url, tmp):
    """Télécharge la vidéo avec yt-dlp. Renvoie (chemin, description)."""
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
        if is_yt:  # Gemini sait lire YouTube directement
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
                    model=model, contents=parts + [PROMPT.format(desc=desc or "(aucune)")],
                    config=types.GenerateContentConfig(response_mime_type="application/json"))
                data = json.loads(resp.text)
                return data[0] if isinstance(data, list) else data
            except Exception as e:  # modèle indispo / quota : on essaie le suivant
                last_err = e
        raise RuntimeError(f"Gemini : {last_err}")


def save(row):
    new = not os.path.exists(CSV_FILE)
    with open(CSV_FILE, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in FIELDS})


def main():
    updates = tg("getUpdates", timeout=0).get("result", [])
    if not updates:
        print("Rien de nouveau.")
        return
    for u in updates:
        msg = u.get("message") or u.get("channel_post") or {}
        chat = msg.get("chat", {}).get("id")
        text = (msg.get("text") or "") + " " + (msg.get("caption") or "")
        if not chat:
            continue
        if ALLOWED and str(chat) != ALLOWED:
            continue
        urls = URL_RE.findall(text)
        if not urls:
            tg("sendMessage", chat_id=chat, text=f"Envoie-moi un lien de réel / TikTok / YouTube.\n(Ton chat id : {chat})")
            continue
        for url in urls:
            try:
                d = analyse(url)
                d.update(date=datetime.date.today().isoformat(), lien=url)
                save(d)
                stars = "⭐" * int(d.get("note", 0) or 0)
                txt = (f"{stars} {d.get('titre','')}\n[{d.get('categorie','')}]\n\n{d.get('resume','')}\n\n"
                       f"👉 {d.get('pourquoi','')}")
                if d.get("a_retenir"):
                    txt += f"\n📌 {d['a_retenir']}"
                tg("sendMessage", chat_id=chat, text=txt, disable_web_page_preview=True)
            except Exception as e:
                tg("sendMessage", chat_id=chat, text=f"❌ Échec pour {url}\n{e}"[:4000])
    # Confirme à Telegram que ces messages sont traités
    tg("getUpdates", offset=updates[-1]["update_id"] + 1, timeout=0)


if __name__ == "__main__":
    main()
