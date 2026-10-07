"""
Blacklist online oyun kayıtlarını (Firebase "logs") indirip SQLite veritabanına yazar.

Kullanım:
    python export_logs.py              -> blacklist.db dosyasını oluşturur / yeniler
    python export_logs.py kayitlar.db  -> başka bir dosya adına yazar

Oluşan dosyayı "DB Browser for SQLite" (https://sqlitebrowser.org) ile açıp
"Execute SQL" sekmesinde sorgu çalıştırabilirsin. Örnekler en altta.

Sadece Python'un kendi kütüphanelerini kullanır, ek kurulum gerekmez.
Her çalıştırmada tablolar baştan oluşturulur (Firebase'deki son durum).
"""

import json
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

# blacklist.html içindeki FIREBASE_CONFIG ile aynı olmalı
API_KEY = "AIzaSyCO42lCczz5G0sM8cN1CFHp85ULHhlAfno"
DATABASE_URL = "https://blacklist-1e5bb-default-rtdb.europe-west1.firebasedatabase.app"

HERE = Path(__file__).resolve().parent
TOKEN_FILE = HERE / ".firebase_token.json"  # aynı anonim kullanıcıyı tekrar kullanmak için


def post_json(url, payload):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def get_id_token():
    """Anonim giriş yapar. Önceki girişin anahtarı varsa yeni kullanıcı açmadan onu yeniler."""
    if TOKEN_FILE.exists():
        try:
            refresh = json.loads(TOKEN_FILE.read_text())["refresh_token"]
            data = post_json(f"https://securetoken.googleapis.com/v1/token?key={API_KEY}",
                             {"grant_type": "refresh_token", "refresh_token": refresh})
            return data["id_token"]
        except Exception:
            pass  # süresi dolduysa yeniden giriş yapılır
    try:
        data = post_json(f"https://identitytoolkit.googleapis.com/v1/accounts:signUp?key={API_KEY}",
                         {"returnSecureToken": True})
    except urllib.error.HTTPError as e:
        msg = json.loads(e.read() or b"{}").get("error", {}).get("message", e.code)
        print(f"Anonim giriş yapılamadı ({msg}). Girişsiz deneniyor.")
        return None
    TOKEN_FILE.write_text(json.dumps({"refresh_token": data["refreshToken"]}))
    return data["idToken"]


def fetch_logs(token):
    url = f"{DATABASE_URL}/logs.json" + (f"?auth={token}" if token else "")
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return json.load(r) or {}
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            sys.exit("Firebase okumaya izin vermedi. Konsolda anonim girişin açık olduğundan emin ol.")
        raise


def as_list(v):
    """Firebase dizileri bazen {"0": .., "1": ..} nesnesi olarak döner."""
    if isinstance(v, list):
        return v
    if isinstance(v, dict):
        return [v[k] for k in sorted(v, key=lambda k: int(k) if str(k).isdigit() else 0)]
    return []


def num(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


SCHEMA = """
DROP TABLE IF EXISTS words;
DROP TABLE IF EXISTS turns;
DROP TABLE IF EXISTS games;

CREATE TABLE games (
  id        TEXT PRIMARY KEY,
  played_at TEXT,      -- yerel saat, 'YYYY-MM-DD HH:MM:SS'
  mode      TEXT,      -- 'duo' (düello) / 'team' (takım)
  room      TEXT,
  rounds    INTEGER,
  duration  INTEGER,   -- tur başına saniye
  difficulty TEXT,     -- 'mix', '1', '2', '3'
  categories TEXT,     -- virgülle
  name0     TEXT, score0 INTEGER,
  name1     TEXT, score1 INTEGER,
  winner    TEXT       -- kazananın adı, berabereyse NULL
);

CREATE TABLE turns (
  game_id   TEXT REFERENCES games(id),
  turn_no   INTEGER,   -- oyundaki sırası, 1'den başlar
  side      INTEGER,   -- 0 / 1
  side_name TEXT,      -- oyuncu ya da takım adı
  narrator  TEXT,
  correct   INTEGER,
  taboo     INTEGER,
  pass      INTEGER,
  points    INTEGER,
  PRIMARY KEY (game_id, turn_no)
);

CREATE TABLE words (
  game_id   TEXT REFERENCES games(id),
  turn_no   INTEGER,
  narrator  TEXT,
  word      TEXT,
  result    TEXT       -- 'taboo' / 'pass' (kayıtlarda doğru bilinen kelimeler tutulmuyor)
);
"""


def main():
    db_path = HERE / (sys.argv[1] if len(sys.argv) > 1 else "blacklist.db")
    logs = fetch_logs(get_id_token())

    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA)
    for gid, g in logs.items():
        if not isinstance(g, dict):
            continue
        names = [str(n) for n in as_list(g.get("names"))] + ["", ""]
        scores = [num(s) for s in as_list(g.get("scores"))] + [0, 0]
        w = num(g.get("winner"))
        date = num(g.get("date"))
        con.execute(
            "INSERT INTO games VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (gid, datetime.fromtimestamp(date / 1000).strftime("%Y-%m-%d %H:%M:%S") if date else None,
             g.get("mode"), g.get("room"), num(g.get("rounds")), num(g.get("duration")), str(g.get("diff", "")),
             ",".join(map(str, as_list(g.get("cats")))),
             names[0], scores[0], names[1], scores[1], names[w] if w in (0, 1) else None),
        )
        for i, t in enumerate(as_list(g.get("turns")), start=1):
            if not isinstance(t, dict):
                continue
            side = num(t.get("side"))
            narrator = str(t.get("narrator", ""))
            con.execute(
                "INSERT INTO turns VALUES (?,?,?,?,?,?,?,?,?)",
                (gid, i, side, names[side] if side in (0, 1) else None, narrator,
                 num(t.get("correct")), num(t.get("taboo")), num(t.get("pass")), num(t.get("points"))),
            )
            for key, result in (("tabooWords", "taboo"), ("passWords", "pass")):
                for word in as_list(t.get(key)):
                    con.execute("INSERT INTO words VALUES (?,?,?,?,?)", (gid, i, narrator, str(word), result))
    con.commit()

    counts = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("games", "turns", "words")}
    con.close()
    print(f"{db_path.name} hazır: {counts['games']} oyun, {counts['turns']} tur, {counts['words']} tabu/pas kelimesi.")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()


# --------------------------------------------------------------------------
# ÖRNEK SORGULAR (DB Browser for SQLite -> Execute SQL)
#
# Son oyunlar:
#   SELECT played_at, name0, score0, score1, name1, winner FROM games ORDER BY played_at DESC;
#
# Oyuncu sıralaması (anlatıcı olarak):
#   SELECT narrator, COUNT(*) AS tur, SUM(correct) AS dogru, SUM(taboo) AS tabu, SUM(points) AS puan
#   FROM turns GROUP BY narrator ORDER BY puan DESC;
#
# En çok tabu yapılan kelimeler:
#   SELECT word, COUNT(*) AS kez FROM words WHERE result = 'taboo' GROUP BY word ORDER BY kez DESC LIMIT 20;
#
# En çok galibiyet:
#   SELECT winner, COUNT(*) AS galibiyet FROM games WHERE winner IS NOT NULL GROUP BY winner ORDER BY galibiyet DESC;
# --------------------------------------------------------------------------
