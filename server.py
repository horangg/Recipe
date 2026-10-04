import base64, json, os, re, sqlite3, subprocess, tempfile, threading, time, uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from google import genai
from google.genai import types
from pydantic import BaseModel

# 앞 모델이 과부하(503)거나 무료 한도 초과(429)면 다음 모델로 (모델마다 한도가 따로)
MODELS = [os.environ.get("GEMINI_MODEL", "gemini-flash-latest"), "gemini-3.8-flash", "gemini-3.5-flash", "gemini-3.6-flash", "gemini-3.1-flash-lite"]
COOKIES = os.environ.get("COOKIES_BROWSER")  # e.g. "chrome" or "safari", for Instagram login walls
TOKEN = os.environ.get("APP_TOKEN")  # 설정하면 모든 /api 요청에 X-Token 헤더 필요 (공개 서버용)
# 인스타 로그인 벽 우회용 쿠키(Netscape 형식). 우선순위: COOKIES_B64(base64 한 줄, 붙여넣기 사고에 안전) > COOKIES_TXT > Secret File
_secret = Path("/etc/secrets/cookies.txt")
COOKIES_ERR = None  # 설정이 잘못돼도 서버는 뜨고, 인스타 요청 때 이유를 알려준다


def load_cookies():
    global COOKIES_ERR
    b64 = os.environ.get("COOKIES_B64")
    if b64:
        try:
            b64 = "".join(b64.split()).strip("\"'")
            return base64.b64decode(b64 + "=" * (-len(b64) % 4)).decode()
        except Exception:
            COOKIES_ERR = f"COOKIES_B64 값이 올바른 base64가 아닙니다 (현재 {len(b64)}자). 다시 복사해 붙여넣으세요."
            return None
    return os.environ.get("COOKIES_TXT") or (_secret.read_text() if _secret.exists() else None)


COOKIES_TXT = load_cookies()
DB_URL = os.environ.get("DATABASE_URL")  # 있으면 Postgres(클라우드), 없으면 로컬 SQLite 파일
DB_FILE = Path(__file__).parent / "recipes.db"
if os.environ.get("RENDER") and not DB_URL:  # Render 디스크는 재시작 때 지워지므로, DB 없이 뜨면 레시피가 조용히 사라진다
    raise SystemExit("DATABASE_URL 환경변수가 필요합니다 (Render Environment에 Postgres 연결 문자열 설정)")
HERE = Path(__file__).parent

client = genai.Client()  # reads GEMINI_API_KEY
app = FastAPI()


class Ingredient(BaseModel):
    name: str
    amount: str


class Step(BaseModel):
    text: str
    timestamp: str  # "mm:ss" in the video where this step is shown, "" if unknown
    ingredients: list[Ingredient]


class Section(BaseModel):
    title: str
    steps: list[Step]


class Recipe(BaseModel):
    title: str
    servings: str
    minutes: int
    ingredients: list[Ingredient]
    sections: list[Section]
    tips: list[str]


PROMPT = """첨부된 요리 영상의 화면, 음성, 자막과 아래 캡션을 모두 종합해 한국어 레시피를 만들어라.
- 캡션에 재료/분량이 있으면 가장 우선 신뢰하고, 영상과 다르면 영상 기준으로 보정.
- 재료는 같은 것끼리 합치고 분량은 단위를 통일(g, ml, 큰술 등). 분량을 알 수 없으면 "적당량".
- 조리 순서는 섹션으로 나누고, 각 단계에 영상의 해당 시점(mm:ss)과 그 단계에 쓰는 재료를 붙여라.
- 영상에 없는 내용은 지어내지 마라.

캡션:
{caption}
"""


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, check=True)


def fetch(url: str, d: str):
    base = ["yt-dlp", "--no-playlist", "--no-warnings"]
    if COOKIES:
        base += ["--cookies-from-browser", COOKIES]
    if COOKIES_ERR:
        raise HTTPException(500, COOKIES_ERR)
    if COOKIES_TXT:
        lines = COOKIES_TXT.splitlines()
        if not any(l.count("\t") == 6 for l in lines):  # 탭 7칸 = 정상 쿠키 줄
            raise HTTPException(500, "쿠키 설정이 올바르지 않습니다 (Netscape 형식 쿠키 줄이 없음). COOKIES_B64를 다시 만드세요.")
        if not any("\tsessionid\t" in l for l in lines):
            raise HTTPException(500, "쿠키에 로그인 정보(sessionid)가 없습니다. 값이 잘렸거나 로그아웃 상태에서 추출했을 수 있습니다.")
        (Path(d) / "cookies.txt").write_text(COOKIES_TXT)
        base += ["--cookies", f"{d}/cookies.txt"]
    try:
        meta = json.loads(run(base + ["--dump-json", url]).stdout)
        run(base + ["-f", "bv*[height<=480]+ba/b[height<=480]/b", "--merge-output-format", "mp4", "-o", f"{d}/v.%(ext)s", url])
    except subprocess.CalledProcessError as e:
        errs = " ".join(l for l in e.stderr.splitlines() if l.startswith("ERROR"))  # 핵심 ERROR 줄만
        raise HTTPException(502, f"영상 다운로드 실패: {(errs or e.stderr.strip())[:400]}")
    video = next(Path(d).glob("v.*"))
    return video, meta.get("description") or meta.get("title") or ""


def make_recipe(url: str) -> dict:
    if re.match(r"https?://(www\.|m\.)?(youtube\.com|youtu\.be)/", url):
        # 유튜브는 다운로드 없이 Gemini가 URL로 직접 시청 (클라우드 IP 차단 회피)
        return json.loads(generate(types.Part(file_data=types.FileData(file_uri=url)), "").text)
    with tempfile.TemporaryDirectory() as d:
        video, caption = fetch(url, d)
        f = client.files.upload(file=str(video))
        while f.state.name == "PROCESSING":
            time.sleep(2)
            f = client.files.get(name=f.name)
        if f.state.name != "ACTIVE":
            raise HTTPException(502, "Gemini 영상 처리 실패")
        try:
            r = generate(f, caption)
        finally:
            client.files.delete(name=f.name)
    return json.loads(r.text)


def generate(f, caption):
    err = None
    for attempt in range(3):
        for model in MODELS:
            try:
                return client.models.generate_content(
                    model=model,
                    contents=[f, PROMPT.format(caption=caption)],
                    config={"response_mime_type": "application/json", "response_schema": Recipe},
                )
            except genai.errors.APIError as e:
                err = e
        time.sleep(3 * (attempt + 1))
    raise HTTPException(502, f"Gemini 오류 {err.code}: {err.message[:200]}")


jobs: dict = {}


def db(sql, args=()):
    """SQLite/Postgres 공용. SQL은 ? 자리표시자로 작성."""
    if DB_URL:
        import psycopg
        con, sql = psycopg.connect(DB_URL, autocommit=True), sql.replace("?", "%s")
    else:
        con = sqlite3.connect(DB_FILE, isolation_level=None)
    try:
        cur = con.execute(sql, args)
        return cur.fetchall() if cur.description else []
    finally:
        con.close()


db("CREATE TABLE IF NOT EXISTS folders (id TEXT PRIMARY KEY, name TEXT NOT NULL, created TEXT NOT NULL)")
db("""CREATE TABLE IF NOT EXISTS recipes (id TEXT PRIMARY KEY, url TEXT UNIQUE NOT NULL, title TEXT NOT NULL,
      data TEXT NOT NULL, folder_id TEXT, created TEXT NOT NULL)""")


def new_id():
    return uuid.uuid4().hex


def now():
    return datetime.now(timezone.utc).isoformat()


def norm(url):  # 같은 영상이 추적 파라미터(igsh 등)만 달라 중복 저장되지 않게
    u = urlparse(url)
    vid = u.path.strip("/").split("/")[-1]
    if u.netloc == "youtu.be" or (u.netloc.endswith("youtube.com") and u.path.startswith("/shorts/")):
        return f"https://www.youtube.com/watch?v={vid}"  # 짧은 링크/쇼츠도 watch 형태로 통일
    q = {k: v for k, v in parse_qs(u.query).items() if k == "v"}
    return urlunparse((u.scheme, u.netloc, u.path.rstrip("/"), "", urlencode(q, doseq=True), ""))


def check(token):
    if TOKEN and token != TOKEN:
        raise HTTPException(401, "접근 토큰이 올바르지 않습니다")


def by_url(url):
    r = db("SELECT id, data, folder_id FROM recipes WHERE url = ?", (url,))
    return {**json.loads(r[0][1]), "id": r[0][0], "folder_id": r[0][2]} if r else None


def work(job_id: str, url: str, folder_id):
    try:
        data = make_recipe(url)
        data["url"] = url
        db("INSERT INTO recipes (id, url, title, data, folder_id, created) VALUES (?,?,?,?,?,?) ON CONFLICT (url) DO NOTHING",
           (new_id(), url, data["title"], json.dumps(data, ensure_ascii=False), folder_id, now()))
        jobs[job_id]["status"], jobs[job_id]["recipe"] = "done", by_url(url)
    except HTTPException as e:
        jobs[job_id].update(status="error", error=e.detail)
    except Exception as e:  # ponytail: 예상 못 한 오류도 화면에 보이게만 함
        jobs[job_id].update(status="error", error=f"{type(e).__name__}: {e}")


@app.post("/api/recipe")
def recipe(body: dict, x_token: str | None = Header(None)):
    check(x_token)
    m = re.search(r"https?://\S+", body.get("url") or "")  # 공유 텍스트에서 링크만 추출
    if not m:
        raise HTTPException(400, "링크를 찾을 수 없습니다")
    url = norm(m.group(0))
    if saved := by_url(url):
        return {"status": "done", "recipe": saved}
    job_id = new_id()
    jobs[job_id] = {"status": "pending", "url": url}
    threading.Thread(target=work, args=(job_id, url, body.get("folder_id")), daemon=True).start()
    return {"status": "pending", "id": job_id}


@app.get("/api/job/{job_id}")
def job(job_id: str, x_token: str | None = Header(None)):
    check(x_token)
    return jobs.get(job_id) or {"status": "error", "error": "작업을 찾을 수 없습니다 (서버가 재시작됐을 수 있음)"}


@app.delete("/api/job/{job_id}")
def dismiss(job_id: str, x_token: str | None = Header(None)):
    check(x_token)
    jobs.pop(job_id, None)
    return {}


@app.get("/api/library")
def library(x_token: str | None = Header(None)):
    check(x_token)
    return {
        "folders": [{"id": r[0], "name": r[1]} for r in db("SELECT id, name FROM folders ORDER BY name")],
        "recipes": [{"id": r[0], "title": r[1], "folder_id": r[2], "created": r[3], "url": r[4]}
                    for r in db("SELECT id, title, folder_id, created, url FROM recipes ORDER BY created DESC")],
        "jobs": [{"id": k, **{x: v[x] for x in ("status", "url", "error") if x in v}}
                 for k, v in jobs.items() if v["status"] != "done"],
    }


@app.get("/api/recipes/{rid}")
def get_recipe(rid: str, x_token: str | None = Header(None)):
    check(x_token)
    r = db("SELECT id, data, folder_id FROM recipes WHERE id = ?", (rid,))
    if not r:
        raise HTTPException(404, "레시피를 찾을 수 없습니다")
    return {**json.loads(r[0][1]), "id": r[0][0], "folder_id": r[0][2]}


@app.patch("/api/recipes/{rid}")
def move_recipe(rid: str, body: dict, x_token: str | None = Header(None)):
    check(x_token)
    db("UPDATE recipes SET folder_id = ? WHERE id = ?", (body.get("folder_id") or None, rid))
    return {}


@app.delete("/api/recipes/{rid}")
def delete_recipe(rid: str, x_token: str | None = Header(None)):
    check(x_token)
    db("DELETE FROM recipes WHERE id = ?", (rid,))
    return {}


@app.post("/api/folders")
def add_folder(body: dict, x_token: str | None = Header(None)):
    check(x_token)
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "폴더 이름이 필요합니다")
    fid = new_id()
    db("INSERT INTO folders (id, name, created) VALUES (?,?,?)", (fid, name, now()))
    return {"id": fid, "name": name}


@app.patch("/api/folders/{fid}")
def rename_folder(fid: str, body: dict, x_token: str | None = Header(None)):
    check(x_token)
    db("UPDATE folders SET name = ? WHERE id = ?", ((body.get("name") or "").strip(), fid))
    return {}


@app.delete("/api/folders/{fid}")
def delete_folder(fid: str, x_token: str | None = Header(None)):
    check(x_token)
    db("UPDATE recipes SET folder_id = NULL WHERE folder_id = ?", (fid,))  # 레시피는 '분류 안 됨'으로 이동
    db("DELETE FROM folders WHERE id = ?", (fid,))
    return {}


@app.get("/")
def index():
    return FileResponse(HERE / "index.html")


@app.get("/manifest.json")
def manifest():
    return FileResponse(HERE / "manifest.json")


@app.get("/icon.png")
def icon():
    return FileResponse(HERE / "icon.png")
