#!/usr/bin/env python3
"""
GitHub Actions 에서 실행 — posts/<post_id>/ 의 사진을 인스타 캐러셀로 발행한다.

  python publish.py --post 20261201-153012     # 그 게시물 하나 (지금 발행)
  python publish.py --scheduled                # 오늘(KST) 이전 예약분 중 안 올라간 것 전부

환경변수 (GitHub Secrets)
  IG_USER_ID       인스타 계정 ID (17841…)
  IG_ACCESS_TOKEN  인스타 토큰 (IGAA… 또는 EAA… — 종류를 보고 주소를 자동으로 고른다)
  IMG_BASE         https://raw.githubusercontent.com/<아이디>/<저장소>/<브랜치>
  GRAPH_BASE       (선택) 주소를 직접 정할 때만. 없으면 IGAA → graph.instagram.com, EAA → graph.facebook.com/v21.0

흐름: 사진마다 자식 컨테이너 → 부모(CAROUSEL) → 처리 대기 → media_publish → posted.txt
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parent
POSTS = ROOT / "posts"
POST_ID_RE = re.compile(r"^\d{8}-\d{6}$")

HINTS = {
    190: "토큰이 만료됐거나 틀렸습니다 → 토큰을 새로 받아 IG_ACCESS_TOKEN 을 교체하세요. (Cannot parse 면 토큰이 잘렸거나 종류가 다른 것 — 가이드 STEP 7)",
    10: "게시 권한이 없습니다 → IGAA 토큰은 instagram_business_content_publish, EAA 토큰은 instagram_content_publish 가 있어야 합니다.",
    200: "권한이 없습니다 → 테스터 초대 수락 · 권한 추가 후 토큰을 다시 받았는지 확인하세요.",
    9004: "인스타가 사진을 못 가져갔습니다 → 저장소가 Public 인지, IMG_BASE 가 맞는지 확인하세요.",
    36003: "사진 비율이 맞지 않습니다 → 4:5(0.8) ~ 1.91:1 만 됩니다.",
    9: "발행 한도(24시간 100건)에 걸렸을 수 있습니다.",
}


class GraphError(Exception):
    pass


def env(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        sys.exit(f"❌ 환경변수 {name} 이 비었습니다 — 저장소 Settings → Secrets 를 확인하세요.")
    return v


GRAPH = "https://graph.instagram.com"


def pick_graph(token: str) -> str:
    """토큰 종류에 맞는 주소. IGAA(인스타 로그인) → graph.instagram.com, EAA(페이스북·시스템 사용자) → graph.facebook.com"""
    if os.environ.get("GRAPH_BASE", "").strip():
        return os.environ["GRAPH_BASE"].strip().rstrip("/")
    return "https://graph.facebook.com/v21.0" if token.startswith("EAA") else "https://graph.instagram.com"


def call(method: str, path: str, token: str, **params) -> dict:
    params["access_token"] = token
    data = urllib.parse.urlencode(params).encode()
    url = f"{GRAPH}/{path.lstrip('/')}"
    if method == "GET":
        req = urllib.request.Request(f"{url}?{data.decode()}")
    else:
        req = urllib.request.Request(url, data=data, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read()).get("error", {})
        except Exception:
            err = {}
        code = err.get("code")
        sub = err.get("error_subcode")
        hint = HINTS.get(sub) or HINTS.get(code) or ""
        raise GraphError(f"Graph API {e.code} · code {code} / {sub} · {err.get('message', '')}\n   {hint}")


def wait_url(url: str, tries: int = 12) -> None:
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=20) as r:
                if r.status == 200:
                    return
        except Exception:
            pass
        time.sleep(5)
    raise GraphError(f"사진 주소가 열리지 않습니다: {url}\n   {HINTS[9004]}")


def wait_finished(cid: str, token: str, tries: int = 30) -> None:
    for _ in range(tries):
        st = call("GET", cid, token, fields="status_code").get("status_code")
        if st == "FINISHED":
            return
        if st in ("ERROR", "EXPIRED"):
            raise GraphError(f"컨테이너 {cid} 처리 실패 ({st}). 사진 형식 · 비율을 확인하세요.")
        time.sleep(3)
    raise GraphError(f"컨테이너 {cid} 처리가 90초 안에 끝나지 않았습니다.")


def publish(post_dir: Path, ig_user: str, token: str, img_base: str) -> None:
    meta = json.loads((post_dir / "meta.json").read_text(encoding="utf-8"))
    done = post_dir / "posted.txt"
    if done.exists():
        print(f"⏭  {post_dir.name} 이미 발행됨 — 건너뜀")
        return
    images = sorted(p.name for p in post_dir.glob("*.jpg"))
    if not 1 <= len(images) <= 10:
        raise GraphError(f"사진이 {len(images)}장입니다 (1~10장).")
    caption = (post_dir / "caption.txt").read_text(encoding="utf-8").strip()
    urls = [f"{img_base.rstrip('/')}/posts/{post_dir.name}/{n}" for n in images]
    print(f"📷 {post_dir.name} ({meta.get('source')}) · {len(urls)}장")
    for u in urls:
        wait_url(u)

    if len(urls) == 1:
        parent = call("POST", f"{ig_user}/media", token, image_url=urls[0], caption=caption)["id"]
    else:
        children = []
        for u in urls:
            cid = call("POST", f"{ig_user}/media", token, image_url=u, is_carousel_item="true")["id"]
            wait_finished(cid, token)
            children.append(cid)
            print(f"   · 자식 컨테이너 {cid}")
        parent = call("POST", f"{ig_user}/media", token, media_type="CAROUSEL",
                      children=",".join(children), caption=caption)["id"]
    print(f"   · 부모 컨테이너 {parent}")
    wait_finished(parent, token)
    media_id = call("POST", f"{ig_user}/media_publish", token, creation_id=parent)["id"]
    try:
        link = call("GET", media_id, token, fields="permalink").get("permalink", "")
    except GraphError:
        link = ""
    done.write_text(f"media_id={media_id}\npermalink={link}\n"
                    f"published_at={datetime.now(KST).isoformat(timespec='seconds')}\n", encoding="utf-8")
    print(f"✅ 발행 완료 media_id={media_id} {link}")

    if meta.get("delete_after"):
        for n in images:
            (post_dir / n).unlink()
        print("   🧹 저장소에서 사진 파일 삭제 (git 기록에는 남습니다)")


def main() -> None:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--post")
    g.add_argument("--scheduled", action="store_true")
    a = ap.parse_args()

    ig_user, token, img_base = env("IG_USER_ID"), env("IG_ACCESS_TOKEN"), env("IMG_BASE")
    global GRAPH
    GRAPH = pick_graph(token)
    print(f"🔑 토큰 종류 {'EAA' if token.startswith('EAA') else 'IGAA'} → {GRAPH}")

    if a.post:
        if not POST_ID_RE.match(a.post):
            sys.exit(f"❌ post_id 형식이 아닙니다: {a.post!r}")
        targets = [POSTS / a.post]
        if not targets[0].is_dir():
            sys.exit(f"❌ posts/{a.post} 가 저장소에 없습니다.")
    else:
        today = datetime.now(KST).date().isoformat()
        targets = []
        for d in sorted(p for p in POSTS.iterdir() if p.is_dir()) if POSTS.is_dir() else []:
            m = d / "meta.json"
            if not m.is_file() or (d / "posted.txt").exists():
                continue
            on = json.loads(m.read_text(encoding="utf-8")).get("publish_on")
            if on and on <= today:
                targets.append(d)
        if not targets:
            print(f"오늘({today}) 발행할 예약 게시물이 없습니다.")
            return

    failed = 0
    for d in targets:
        try:
            publish(d, ig_user, token, img_base)
        except GraphError as e:
            failed += 1
            print(f"❌ {d.name} 발행 실패\n   {e}")
    if failed:
        sys.exit(1)  # 실패하면 GitHub 가 메일로 알려 준다


if __name__ == "__main__":
    main()
