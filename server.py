import hashlib, json, os, re, subprocess, tempfile, threading, time, uuid
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from google import genai
from google.genai import types
from pydantic import BaseModel

MODELS = [os.environ.get("GEMINI_MODEL", "gemini-flash-latest"), "gemini-3.8-flash"]  # 앞 모델이 과부하면 다음 모델로
COOKIES = os.environ.get("COOKIES_BROWSER")  # e.g. "chrome" or "safari", for Instagram login walls
TOKEN = os.environ.get("APP_TOKEN")  # 설정하면 모든 /api 요청에 X-Token 헤더 필요 (공개 서버용)
COOKIES_TXT = os.environ.get("COOKIES_TXT")  # Netscape 형식 쿠키 내용 (클라우드에서 인스타 로그인 벽 우회용, 선택)
CACHE = Path(__file__).parent / "cache"
CACHE.mkdir(exist_ok=True)

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
    if COOKIES_TXT:
        (Path(d) / "cookies.txt").write_text(COOKIES_TXT)
        base += ["--cookies", f"{d}/cookies.txt"]
    try:
        meta = json.loads(run(base + ["--dump-json", url]).stdout)
        run(base + ["-f", "bv*[height<=480]+ba/b[height<=480]/b", "--merge-output-format", "mp4", "-o", f"{d}/v.%(ext)s", url])
    except subprocess.CalledProcessError as e:
        raise HTTPException(502, f"영상 다운로드 실패: {e.stderr.strip()[-300:]}")
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


def check(token):
    if TOKEN and token != TOKEN:
        raise HTTPException(401, "접근 토큰이 올바르지 않습니다")


def work(job_id: str, url: str, path: Path):
    try:
        data = make_recipe(url)
        data["url"] = url
        path.write_text(json.dumps(data, ensure_ascii=False))
        jobs[job_id] = {"status": "done", "recipe": data}
    except HTTPException as e:
        jobs[job_id] = {"status": "error", "error": e.detail}
    except Exception as e:  # ponytail: 예상 못 한 오류도 화면에 보이게만 함
        jobs[job_id] = {"status": "error", "error": f"{type(e).__name__}: {e}"}


@app.post("/api/recipe")
def recipe(body: dict, x_token: str | None = Header(None)):
    check(x_token)
    m = re.search(r"https?://\S+", body.get("url") or "")  # 공유 텍스트에서 링크만 추출
    if not m:
        raise HTTPException(400, "링크를 찾을 수 없습니다")
    url = m.group(0)
    path = CACHE / (hashlib.sha1(url.encode()).hexdigest() + ".json")
    if path.exists():
        return {"status": "done", "recipe": json.loads(path.read_text())}
    job_id = uuid.uuid4().hex
    jobs[job_id] = {"status": "pending"}
    threading.Thread(target=work, args=(job_id, url, path), daemon=True).start()
    return {"status": "pending", "id": job_id}


@app.get("/api/job/{job_id}")
def job(job_id: str, x_token: str | None = Header(None)):
    check(x_token)
    return jobs.get(job_id) or {"status": "error", "error": "작업을 찾을 수 없습니다 (서버가 재시작됐을 수 있음)"}


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "index.html")
