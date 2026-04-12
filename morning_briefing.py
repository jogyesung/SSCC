#!/usr/bin/env python3
"""
사우스스프링스 골프 모닝브리핑 시스템
골프 산업 뉴스, 대회, 장비/시장, 정책/규제 및 날씨 정보를 수집하여
경영진용 HTML 브리핑을 생성하고 이메일로 발송합니다.
"""

import json
import os
import re
import sys
import smtplib
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import quote, urlparse, parse_qs, urlencode

import anthropic
import feedparser
import requests
from bs4 import BeautifulSoup

# ──────────────────────────────────────────────
# 설정 & 유틸리티
# ──────────────────────────────────────────────

KST = timezone(timedelta(hours=9))
WEEKDAY_KR = ["월", "화", "수", "목", "금", "토", "일"]

# 모듈 전역 Anthropic 클라이언트 (재사용)
_anthropic_client = None


def _get_client(api_key):
    """Anthropic 클라이언트 lazy singleton"""
    global _anthropic_client
    if _anthropic_client is None and api_key:
        _anthropic_client = anthropic.Anthropic(api_key=api_key)
    return _anthropic_client

WEATHER_ICON_MAP = {
    "Clear": "☀️",
    "Clouds": "☁️",
    "Rain": "🌧️",
    "Drizzle": "🌦️",
    "Thunderstorm": "⛈️",
    "Snow": "❄️",
    "Mist": "🌫️",
    "Fog": "🌫️",
    "Haze": "🌫️",
}


def load_config():
    """config.json 로드 + 환경변수 오버라이드"""
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
    else:
        config = {}

    # 환경변수 오버라이드 (GitHub Actions용)
    if os.environ.get("CLAUDE_API_KEY"):
        config["claude_api_key"] = os.environ["CLAUDE_API_KEY"]
    if os.environ.get("EMAIL_PASSWORD"):
        config["email_password"] = os.environ["EMAIL_PASSWORD"]
    if os.environ.get("GMAIL_APP_PASSWORD"):
        config["gmail_app_password"] = os.environ["GMAIL_APP_PASSWORD"]
    if os.environ.get("SMTP_HOST"):
        config["smtp_host"] = os.environ["SMTP_HOST"]
    if os.environ.get("SMTP_PORT"):
        config["smtp_port"] = os.environ["SMTP_PORT"]
    if os.environ.get("EMAIL_FROM"):
        config["email_from"] = os.environ["EMAIL_FROM"]
    if os.environ.get("EMAIL_TO"):
        config["email_to"] = os.environ["EMAIL_TO"]
    if os.environ.get("WEATHER_API_KEY"):
        config.setdefault("weather", {})["api_key"] = os.environ["WEATHER_API_KEY"]

    # 기본값 설정
    config.setdefault("golf_course", {
        "name": "사우스스프링스",
        "name_en": "South Springs",
        "location": "경기도 이천",
        "lat": 37.28,
        "lon": 127.43,
    })
    config.setdefault("news_categories", {
        "industry": {
            "label": "골프장 산업 동향",
            "keywords_kr": ["골프장 경영", "골프장 산업", "골프 리조트", "골프장 개장", "골프장 회원권", "골프장 매출"],
            "keywords_en": ["golf course industry", "golf resort business"],
        },
        "tournament": {
            "label": "골프 대회/투어",
            "keywords_kr": ["PGA 투어", "LPGA", "KPGA", "KLPGA", "골프 대회", "마스터즈", "US오픈 골프"],
            "keywords_en": ["PGA Tour", "LPGA Tour"],
        },
        "equipment": {
            "label": "골프 장비/시장",
            "keywords_kr": ["골프 장비", "골프 용품", "골프 시장", "골프웨어", "골프 브랜드"],
            "keywords_en": ["golf equipment market", "golf industry market"],
        },
        "policy": {
            "label": "골프 정책/규제",
            "keywords_kr": ["골프장 규제", "골프 정책", "골프장 세금", "체육시설법", "골프장 환경"],
            "keywords_en": ["golf regulation Korea"],
        },
        "global": {
            "label": "해외 골프 뉴스",
            "keywords_en": ["golf industry news", "PGA Tour", "golf course management", "golf business"],
            "lang": "en",
        },
    })
    config.setdefault("max_articles_per_category", 8)
    config.setdefault("max_age_days", 2)
    config.setdefault("blocked_domains", ["pinterest.com", "youtube.com"])

    return config


def _gn(query, hl="ko", gl="KR"):
    """Google News RSS URL 생성"""
    return f"https://news.google.com/rss/search?q={quote(query)}&hl={hl}&gl={gl}&ceid={gl}:{hl}"


def _is_korean(text):
    """텍스트에 한국어가 포함되어 있는지 확인"""
    return bool(re.search(r"[\uac00-\ud7a3]", text))


# 중복 판별 시 제외할 불용어 (너무 흔해서 변별력 없음)
_STOPWORDS = {
    "골프", "골프장", "골프리조트", "리조트", "골프업계", "뉴스", "기자", "보도",
    "특파원", "속보", "단독", "종합", "영상", "사진", "인터뷰", "이슈",
    "golf", "news", "the", "and", "for", "with", "from", "that", "this",
    "course", "tour", "pga", "lpga",
}


def _title_tokens(title):
    """제목에서 비교용 핵심 토큰 추출 (불용어 제거)"""
    # 신문사명 제거 (' - 신문사명' 패턴)
    cleaned_title = re.sub(r"\s*[-–—]\s*[^-–—]*$", "", title)
    # 특수문자 제거
    cleaned = re.sub(r"[^\w\s]", " ", cleaned_title)
    # 토큰 추출: 2글자 이상, 불용어 제외
    tokens = set()
    for t in cleaned.split():
        t = t.lower()
        if len(t) >= 2 and t not in _STOPWORDS:
            tokens.add(t)
    return tokens


def _is_similar_title(new_title, existing_titles, threshold=0.3):
    """기존 제목들과 유사도 비교. threshold 이상 겹치거나
    핵심 키워드 3개 이상 겹치면 중복으로 판단"""
    new_tokens = _title_tokens(new_title)
    if not new_tokens or len(new_tokens) < 2:
        return False

    for existing in existing_titles:
        existing_tokens = _title_tokens(existing)
        if not existing_tokens or len(existing_tokens) < 2:
            continue
        intersection = new_tokens & existing_tokens
        union = new_tokens | existing_tokens
        jaccard = len(intersection) / len(union)
        # Jaccard 유사도 or 핵심 토큰 3개 이상 공통
        if jaccard >= threshold or len(intersection) >= 3:
            return True
    return False


def _is_junk_title(title):
    """쓰레기 기사 제목 필터 (스크린샷, 파일명 등)"""
    if not title or len(title) < 5:
        return True
    # 스크린샷 파일명 패턴
    if re.match(r"^\s*screenshot", title, re.IGNORECASE):
        return True
    # 제목이 타임스탬프로만 구성된 경우
    if re.match(r"^\s*\d{4}[-./]\d{2}[-./]\d{2}", title):
        return True
    # 확장자가 있는 경우 (이미지/문서 파일명)
    if re.search(r"\.(jpg|jpeg|png|gif|pdf|docx?)(\s|$|-)", title, re.IGNORECASE):
        return True
    return False


def _normalize_url(url):
    """URL 정규화 (추적 파라미터 제거)"""
    parsed = urlparse(url)
    # Google News 리다이렉트 URL에서 실제 URL 추출
    if "news.google.com" in parsed.netloc:
        return url  # Google News URL은 그대로 사용 (리다이렉트)
    # 일반 URL은 쿼리 파라미터 제거
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def _strip_html(text):
    """HTML 태그 제거 및 공백 정리"""
    if not text:
        return ""
    try:
        cleaned = BeautifulSoup(text, "html.parser").get_text(" ", strip=True)
    except Exception:
        cleaned = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", cleaned).strip()


_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# 기사 본문 후보 CSS 선택자 (한국 언론사 공통 패턴)
_ARTICLE_SELECTORS = [
    "article",
    "div#articleBody",
    "div#article-body",
    "div#articleBodyContents",
    "div#newsct_article",
    "div#dic_area",
    "div.article_body",
    "div.article-body",
    "div.article_view",
    "div.article-content",
    "div.news_view",
    "div.news-body",
    "div.art_body",
    "div.view_con",
    "div.view_content",
    "section.article",
    "main",
]


def fetch_article_content(url, timeout=8, max_chars=1500):
    """기사 URL에서 본문 텍스트를 추출. 실패 시 빈 문자열.

    Google News 리다이렉트 URL의 경우 requests가 HTTP 리다이렉트를 따라가며,
    최종 기사 페이지의 DOM에서 흔한 본문 컨테이너를 찾아 텍스트를 반환한다.
    """
    if not url:
        return ""
    try:
        headers = {
            "User-Agent": _USER_AGENT,
            "Accept-Language": "ko,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml",
        }
        resp = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        if resp.status_code != 200 or not resp.text:
            return ""
        resp.encoding = resp.encoding or resp.apparent_encoding

        soup = BeautifulSoup(resp.text, "html.parser")

        # 최종 페이지가 여전히 Google News 리다이렉트면 포기
        if "news.google.com" in (urlparse(resp.url).netloc or ""):
            return ""

        # 잡음 요소 제거
        for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form", "iframe", "noscript"]):
            tag.decompose()

        # 본문 후보 중 가장 긴 텍스트 블록 선택
        best_text = ""
        for selector in _ARTICLE_SELECTORS:
            for node in soup.select(selector):
                text = node.get_text(" ", strip=True)
                if len(text) > len(best_text):
                    best_text = text

        # Fallback: <p> 태그 집합
        if len(best_text) < 200:
            paragraphs = [
                p.get_text(" ", strip=True)
                for p in soup.find_all("p")
                if len(p.get_text(strip=True)) > 20
            ]
            joined = " ".join(paragraphs)
            if len(joined) > len(best_text):
                best_text = joined

        best_text = re.sub(r"\s+", " ", best_text).strip()
        return best_text[:max_chars]
    except Exception:
        return ""


def enrich_with_content(articles_groups, max_workers=10):
    """모든 기사(카테고리 dict + self_news 리스트)의 본문을 병렬 fetch하여
    각 article dict에 "content" 필드를 추가한다.

    articles_groups: list of iterables (카테고리별 list 또는 self_news list)
    """
    all_items = []
    for group in articles_groups:
        all_items.extend(group)

    if not all_items:
        return 0

    print(f"  기사 본문 {len(all_items)}건 병렬 수집 중...")
    success = 0
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(fetch_article_content, a["link"]): a for a in all_items}
        for future in as_completed(futures):
            article = futures[future]
            try:
                content = future.result()
            except Exception:
                content = ""
            if content and len(content) > 150:
                article["content"] = content
                success += 1
    print(f"  본문 수집 성공: {success}/{len(all_items)}건")
    return success


# ──────────────────────────────────────────────
# 날씨 수집
# ──────────────────────────────────────────────

def get_current_weather(config):
    """OpenWeatherMap 현재 날씨 조회"""
    weather_config = config.get("weather", {})
    api_key = weather_config.get("api_key", "")
    if not api_key or api_key == "YOUR_OPENWEATHERMAP_API_KEY":
        print("[날씨] API 키가 설정되지 않았습니다.")
        return None

    golf = config["golf_course"]
    url = "https://api.openweathermap.org/data/2.5/weather"
    params = {
        "lat": golf["lat"],
        "lon": golf["lon"],
        "appid": api_key,
        "units": weather_config.get("units", "metric"),
        "lang": "kr",
    }

    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return {
            "temp": data["main"]["temp"],
            "feels_like": data["main"]["feels_like"],
            "humidity": data["main"]["humidity"],
            "wind_speed": data["wind"]["speed"],
            "description": data["weather"][0]["description"],
            "main": data["weather"][0]["main"],
            "sunrise": datetime.fromtimestamp(data["sys"]["sunrise"], tz=KST).strftime("%H:%M"),
            "sunset": datetime.fromtimestamp(data["sys"]["sunset"], tz=KST).strftime("%H:%M"),
        }
    except Exception as e:
        print(f"[날씨] 현재 날씨 조회 실패: {e}")
        return None


def get_weather_forecast(config):
    """OpenWeatherMap 5일 예보 조회 → 일별 요약"""
    weather_config = config.get("weather", {})
    api_key = weather_config.get("api_key", "")
    if not api_key or api_key == "YOUR_OPENWEATHERMAP_API_KEY":
        return None

    golf = config["golf_course"]
    url = "https://api.openweathermap.org/data/2.5/forecast"
    params = {
        "lat": golf["lat"],
        "lon": golf["lon"],
        "appid": api_key,
        "units": weather_config.get("units", "metric"),
        "lang": "kr",
    }

    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        # 3시간 단위 데이터를 일별로 집계
        daily = {}
        for item in data["list"]:
            dt = datetime.fromtimestamp(item["dt"], tz=KST)
            date_str = dt.strftime("%m/%d")
            day_key = dt.strftime("%Y-%m-%d")

            if day_key not in daily:
                daily[day_key] = {
                    "date": date_str,
                    "weekday": WEEKDAY_KR[dt.weekday()],
                    "temps": [],
                    "conditions": [],
                    "rain_probs": [],
                    "wind_speeds": [],
                    "humidity": [],
                }

            d = daily[day_key]
            d["temps"].append(item["main"]["temp"])
            d["conditions"].append(item["weather"][0]["main"])
            d["rain_probs"].append(item.get("pop", 0) * 100)
            d["wind_speeds"].append(item["wind"]["speed"])
            d["humidity"].append(item["main"]["humidity"])

        # 일별 요약 생성
        forecast = []
        for day_key in sorted(daily.keys())[:5]:
            d = daily[day_key]
            # 가장 많이 나온 날씨 상태
            main_condition = max(set(d["conditions"]), key=d["conditions"].count)
            forecast.append({
                "date": d["date"],
                "weekday": d["weekday"],
                "high": round(max(d["temps"]), 1),
                "low": round(min(d["temps"]), 1),
                "condition": main_condition,
                "icon": WEATHER_ICON_MAP.get(main_condition, "🌤️"),
                "rain_prob": round(max(d["rain_probs"])),
                "wind": round(sum(d["wind_speeds"]) / len(d["wind_speeds"]), 1),
                "humidity": round(sum(d["humidity"]) / len(d["humidity"])),
            })

        return forecast
    except Exception as e:
        print(f"[날씨] 예보 조회 실패: {e}")
        return None


def format_golf_weather(current, forecast):
    """골프 라운딩 적합도 평가"""
    if not current:
        return "정보 없음"

    temp = current["temp"]
    wind = current["wind_speed"]
    humidity = current["humidity"]

    # 점수 계산 (100점 만점)
    score = 100

    # 기온 (15~25도 최적)
    if temp < 5 or temp > 35:
        score -= 40
    elif temp < 10 or temp > 30:
        score -= 20
    elif temp < 15 or temp > 25:
        score -= 5

    # 바람 (m/s, 5 이하 최적)
    if wind > 15:
        score -= 30
    elif wind > 10:
        score -= 20
    elif wind > 5:
        score -= 10

    # 습도 (40~70% 최적)
    if humidity > 90:
        score -= 15
    elif humidity > 80:
        score -= 10

    if score >= 80:
        return "최적"
    elif score >= 60:
        return "양호"
    elif score >= 40:
        return "보통"
    else:
        return "부적합"


# ──────────────────────────────────────────────
# 뉴스 수집
# ──────────────────────────────────────────────

def fetch_rss(url, limit=8, max_age_days=3, blocked_domains=None):
    """RSS 피드 수집 및 필터링"""
    blocked_domains = blocked_domains or []
    articles = []

    try:
        feed = feedparser.parse(url)
        if feed.bozo and not feed.entries:
            print(f"[뉴스] RSS 파싱 오류: {url[:80]}...")
            return articles

        now = datetime.now(tz=KST)
        cutoff = now - timedelta(days=max_age_days)

        for entry in feed.entries[:limit * 2]:  # 여유분 확보
            # 발행일 파싱
            published = None
            if hasattr(entry, "published_parsed") and entry.published_parsed:
                try:
                    published = datetime(*entry.published_parsed[:6], tzinfo=KST)
                except Exception:
                    published = now

            # 날짜 필터
            if published and published < cutoff:
                continue

            # 도메인 필터
            link = entry.get("link", "")
            domain = urlparse(link).netloc.lower()
            if any(bd in domain for bd in blocked_domains):
                continue

            # 쓰레기 제목 필터 (스크린샷, 파일명 등)
            title = entry.get("title", "제목 없음")
            if _is_junk_title(title):
                continue

            # 소스 추출
            source = ""
            if hasattr(entry, "source") and hasattr(entry.source, "title"):
                source = entry.source.title
            elif domain:
                source = domain.replace("www.", "")

            articles.append({
                "title": title,
                "link": link,
                "published": published.strftime("%m/%d %H:%M") if published else "",
                "source": source,
            })

            if len(articles) >= limit:
                break

    except Exception as e:
        print(f"[뉴스] RSS 수집 오류: {e}")

    return articles


def collect_news(config):
    """카테고리별 뉴스 수집 + 중복 제거 (병렬 RSS fetch)"""
    categories = config.get("news_categories", {})
    max_articles = config.get("max_articles_per_category", 8)
    max_age = config.get("max_age_days", 3)
    blocked = config.get("blocked_domains", [])

    # 모든 (카테고리, URL) 태스크 빌드
    tasks = []
    for cat_key, cat_config in categories.items():
        lang = cat_config.get("lang", "ko")
        if lang == "en":
            keywords = cat_config.get("keywords_en", [])
        else:
            keywords = cat_config.get("keywords_kr", []) + cat_config.get("keywords_en", [])
        for keyword in keywords:
            if lang == "en":
                url = _gn(keyword, hl="en", gl="US")
            else:
                url = _gn(keyword)
            tasks.append((cat_key, url))

    print(f"  전체 {len(tasks)}개 RSS 피드 병렬 수집 중...")

    # 병렬 fetch (카테고리 순서 유지 위해 인덱스로 결과 정렬)
    fetched = [None] * len(tasks)
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(fetch_rss, url, max_articles, max_age, blocked): i
            for i, (_, url) in enumerate(tasks)
        }
        for future in as_completed(futures):
            i = futures[future]
            try:
                fetched[i] = future.result()
            except Exception as e:
                print(f"[뉴스] RSS fetch 실패: {e}")
                fetched[i] = []

    # 카테고리별 병합 + 전역 중복 제거
    all_news = {cat_key: [] for cat_key in categories}
    seen_urls = set()
    seen_titles = []

    for i, (cat_key, _) in enumerate(tasks):
        for article in (fetched[i] or []):
            normalized = _normalize_url(article["link"])
            title = article["title"]
            if normalized in seen_urls:
                continue
            if _is_similar_title(title, seen_titles):
                continue
            seen_urls.add(normalized)
            seen_titles.append(title)
            all_news[cat_key].append(article)

    for cat_key, cat_config in categories.items():
        label = cat_config.get("label", cat_key)
        all_news[cat_key] = all_news[cat_key][:max_articles]
        print(f"  [{label}] {len(all_news[cat_key])}건 수집")

    return all_news


def collect_golf_course_news(config):
    """자사(사우스스프링스) 관련 뉴스 모니터링 (병렬 RSS fetch)"""
    golf = config["golf_course"]
    max_age = config.get("max_age_days", 3)
    blocked = config.get("blocked_domains", [])

    keywords = [golf["name"], f"{golf['location']} 골프장"]
    urls = [_gn(k) for k in keywords]

    print(f"  [자사 뉴스] {golf['name']} 관련 뉴스 병렬 수집 중...")

    fetched_lists = [[]] * len(urls)
    with ThreadPoolExecutor(max_workers=len(urls)) as executor:
        futures = {
            executor.submit(fetch_rss, url, 5, max_age, blocked): i
            for i, url in enumerate(urls)
        }
        for future in as_completed(futures):
            i = futures[future]
            try:
                fetched_lists[i] = future.result()
            except Exception as e:
                print(f"[자사 뉴스] 오류: {e}")
                fetched_lists[i] = []

    articles = []
    seen_urls = set()
    seen_titles = []
    for lst in fetched_lists:
        for article in lst:
            normalized = _normalize_url(article["link"])
            title = article["title"]
            if normalized not in seen_urls and not _is_similar_title(title, seen_titles):
                seen_urls.add(normalized)
                seen_titles.append(title)
                articles.append(article)

    print(f"  [자사 뉴스] {len(articles)}건 수집 완료")
    return articles[:5]


# ──────────────────────────────────────────────
# AI 처리 (Claude)
# ──────────────────────────────────────────────

def process_articles(articles, api_key, label="", is_global=False):
    """단일 Claude Haiku 호출로 중복 제거 + 요약(+해외 제목 번역)을 일괄 수행.
    이전 dedup_with_ai + summarize_korean_articles + summarize_global_articles를 통합한 함수."""
    if not articles or not api_key:
        return articles

    # 각 기사의 제목 + 본문 일부(있다면)를 프롬프트에 포함
    def _fmt(i, a):
        line = f"{i+1}. 제목: {a['title']}"
        content = a.get("content", "")
        if content:
            # 입력 토큰 절약 위해 본문 앞부분만 사용
            line += f"\n   본문: {content[:700]}"
        return line

    numbered = "\n\n".join(_fmt(i, a) for i, a in enumerate(articles))

    if is_global:
        prompt = f"""다음은 해외 골프 뉴스 기사 목록입니다. 각 기사는 제목과 (가능한 경우) 본문 일부가 제공됩니다.

작업:
(1) 같은 사건/주제를 다룬 기사는 그룹화하여 가장 정보가 풍부한 1개만 남기세요.
(2) 남긴 기사 각각에 대해:
    - 한국어 제목 번역을 작성하세요.
    - 본문에 담긴 구체적 사실(누가·무엇을·어떻게·수치 등)을 한국어 1-2문장으로 요약하세요.
    - 제목을 말 바꿔 쓰지 말고, 본문에서 얻은 새로운 정보를 드러내세요.
    - 본문이 없으면 제목에서 유추할 수 있는 맥락을 간결히 제공하세요.

반드시 아래 형식으로만 출력하세요. 설명·머리말·꼬리말 금지:
===번호===
제목: 한국어 제목 번역
요약: 국문 요약 1-2문장

{numbered}"""
    else:
        prompt = f"""다음은 골프 관련 뉴스 기사 목록입니다. 각 기사는 제목과 (가능한 경우) 본문 일부가 제공됩니다.

작업:
(1) 같은 사건/주제 기사는 그룹화하여 가장 정보가 풍부하고 구체적인 1개만 남기세요.
(2) 남긴 기사마다 **본문에 담긴 구체적 사실**(수치·장소·인물·배경·맥락)을 한국어 1문장(50자 이내)으로 요약하세요.
    - 제목을 말만 바꿔서 반복하지 마세요. 제목에 없는 정보를 반드시 1개 이상 포함하세요.
    - 본문이 제공되지 않은 경우에만 제목에서 유추한 핵심을 제공하세요.
    - 경영진이 기사 전체를 읽지 않고도 본질을 파악할 수 있게 작성하세요.

판단 기준: 같은 프로젝트/사건/대회/제품/주체를 다루면 동일 그룹.

반드시 아래 형식으로만 출력하세요. 설명·머리말·꼬리말 금지:
===번호===
(본문 기반 요약 1문장)

{numbered}"""

    try:
        client = _get_client(api_key)
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        result = response.content[0].text.strip()

        kept = {}
        current_idx = None
        for raw_line in result.split("\n"):
            line = raw_line.strip()
            if not line:
                continue
            tag_match = re.match(r"===\s*(\d+)\s*===", line)
            if tag_match:
                idx = int(tag_match.group(1)) - 1
                if 0 <= idx < len(articles):
                    current_idx = idx
                    kept.setdefault(current_idx, {})
                else:
                    current_idx = None
                continue
            if current_idx is None:
                continue
            if is_global:
                m = re.match(r"제목\s*[:：]\s*(.+)", line)
                if m:
                    kept[current_idx]["title_kr"] = m.group(1).strip().strip("[]")
                    continue
                m = re.match(r"요약\s*[:：]\s*(.+)", line)
                if m:
                    kept[current_idx]["summary_kr"] = m.group(1).strip()
                    continue
            else:
                if "summary_kr" not in kept[current_idx]:
                    kept[current_idx]["summary_kr"] = line

        if not kept:
            print(f"  [{label}] AI 응답 파싱 실패, 원본 유지")
            return articles

        result_articles = []
        for idx in sorted(kept.keys()):
            merged = dict(articles[idx])
            merged.update(kept[idx])
            result_articles.append(merged)

        removed = len(articles) - len(result_articles)
        if removed > 0:
            print(f"  [{label}] 중복 {removed}건 제거 + 요약 완료 ({len(articles)}→{len(result_articles)})")
        else:
            print(f"  [{label}] 요약 완료 ({len(result_articles)}건)")
        return result_articles
    except Exception as e:
        print(f"  [{label}] AI 처리 실패: {e}")
        return articles


def generate_analysis(config, weather_data, news, self_news):
    """경영진 핵심 포인트 생성 (Claude Haiku)"""
    api_key = config.get("claude_api_key", "")
    if not api_key or api_key == "YOUR_ANTHROPIC_API_KEY":
        print("[AI] Claude API 키가 설정되지 않았습니다. 분석을 건너뜁니다.")
        return ""

    today = datetime.now(tz=KST)
    today_str = f"{today.strftime('%Y년 %m월 %d일')} ({WEEKDAY_KR[today.weekday()]})"

    # 날씨 요약
    weather_summary = "날씨 정보 없음"
    if weather_data:
        w = weather_data
        weather_summary = f"기온 {w['temp']}°C (체감 {w['feels_like']}°C), {w['description']}, 바람 {w['wind_speed']}m/s, 습도 {w['humidity']}%"

    # 카테고리별 제목+요약 정리 (요약이 있으면 활용)
    categories_config = config.get("news_categories", {})

    def _line(article, is_global=False):
        title = article.get("title_kr") if is_global else None
        title = title or article["title"]
        summary = article.get("summary_kr", "")
        return f"- {title} ({summary})" if summary else f"- {title}"

    sections = []
    for cat_key, articles in news.items():
        label = categories_config.get(cat_key, {}).get("label", cat_key)
        is_global = (cat_key == "global")
        lines = [_line(a, is_global) for a in articles[:5]]
        if lines:
            sections.append(f"[{label}]\n" + "\n".join(lines))

    news_text = "\n\n".join(sections) if sections else "수집된 뉴스 없음"

    # 자사 뉴스
    self_text = ""
    if self_news:
        self_text = "\n[자사 관련 뉴스]\n" + "\n".join(_line(a) for a in self_news)

    prompt = f"""당신은 사우스스프링스 골프장 경영진을 위한 브리핑 분석가입니다.
오늘 날짜: {today_str}

아래 수집된 골프 업계 뉴스를 분석하여 경영진이 주목할 핵심 사항 3-5개를
간결하게 정리해주세요. 각 항목은 1-2문장으로 작성하세요.

[날씨 요약]
{weather_summary}

{news_text}
{self_text}

형식: HTML <li> 태그로 출력. <ul>이나 </ul>은 포함하지 마세요."""

    try:
        client = _get_client(api_key)
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip()
    except Exception as e:
        print(f"[AI] 분석 생성 실패: {e}")
        return ""


# ──────────────────────────────────────────────
# HTML 생성
# ──────────────────────────────────────────────

def _build_news_section(title, icon, articles, bg_color="#ffffff", is_global=False):
    """뉴스 카테고리 HTML 섹션 생성"""
    if not articles:
        return ""

    rows = ""
    for article in articles:
        source_badge = f'<span style="color:#888;font-size:12px;">{article["source"]}</span>' if article["source"] else ""
        date_badge = f'<span style="color:#aaa;font-size:11px;margin-left:8px;">{article["published"]}</span>' if article["published"] else ""

        # 해외 뉴스: 한국어 번역 제목 우선
        display_title = article.get("title_kr", article["title"]) if is_global else article["title"]

        # 원문 제목 (해외 뉴스만)
        original_title = ""
        if is_global and article.get("title_kr"):
            original_title = f'<div style="color:#999;font-size:11px;margin-top:2px;">{article["title"]}</div>'

        # 요약: AI 생성 요약(summary_kr) 사용
        summary_text = article.get("summary_kr", "")

        summary_html = ""
        if summary_text:
            summary_html = f'<div style="color:#555;font-size:13px;margin-top:3px;padding:4px 8px;background:#f8f9fa;border-left:3px solid #1a5632;border-radius:2px;line-height:1.4;">{summary_text}</div>'

        rows += f"""
        <tr>
          <td style="padding:6px 14px;border-bottom:1px solid #f0f0f0;font-size:14px;line-height:1.4;">
            <a href="{article['link']}" style="color:#1a1a1a;text-decoration:none;" target="_blank">{display_title}</a>
            {original_title}
            {summary_html}
            <div style="margin-top:2px;">{source_badge}{date_badge}</div>
          </td>
        </tr>"""

    return f"""
    <table width="700" cellpadding="0" cellspacing="0" border="0" align="center" style="margin-bottom:10px;border-radius:8px;overflow:hidden;">
      <tr>
        <td bgcolor="{bg_color}" style="padding:0;">
          <table width="100%" cellpadding="0" cellspacing="0" border="0">
            <tr>
              <td style="padding:10px 14px;font-size:17px;font-weight:bold;color:#1a5632;border-bottom:2px solid #e8f5e9;">
                {icon} {title}
                <span style="font-size:13px;color:#888;font-weight:normal;margin-left:8px;">{len(articles)}건</span>
              </td>
            </tr>
            {rows}
          </table>
        </td>
      </tr>
    </table>"""


def generate_briefing(config, current_weather, forecast, news, self_news, analysis):
    """전체 HTML 브리핑 생성"""
    now = datetime.now(tz=KST)
    date_str = now.strftime("%Y년 %m월 %d일")
    weekday = WEEKDAY_KR[now.weekday()]
    golf = config["golf_course"]
    categories_config = config.get("news_categories", {})

    # ── 날씨 섹션 ──
    weather_section = ""
    if current_weather:
        playability = format_golf_weather(current_weather, forecast)
        playability_color = {
            "최적": "#2e7d32", "양호": "#558b2f",
            "보통": "#f9a825", "부적합": "#c62828",
        }.get(playability, "#666")

        weather_section = f"""
    <table width="700" cellpadding="0" cellspacing="0" border="0" align="center" style="margin-bottom:10px;border-radius:8px;overflow:hidden;">
      <tr>
        <td bgcolor="#ffffff" style="padding:0;">
          <table width="100%" cellpadding="0" cellspacing="0" border="0">
            <tr>
              <td style="padding:10px 14px;font-size:17px;font-weight:bold;color:#1a5632;border-bottom:2px solid #e8f5e9;">
                🌤️ 오늘의 날씨 — {golf['location']}
              </td>
            </tr>
            <tr>
              <td style="padding:10px 14px;">
                <table width="100%" cellpadding="4" cellspacing="0" border="0">
                  <tr>
                    <td style="font-size:14px;width:50%;">
                      <b>현재 기온:</b> {current_weather['temp']}°C (체감 {current_weather['feels_like']}°C)<br>
                      <b>날씨:</b> {current_weather['description']}<br>
                      <b>바람:</b> {current_weather['wind_speed']} m/s<br>
                      <b>습도:</b> {current_weather['humidity']}%
                    </td>
                    <td style="font-size:14px;width:50%;">
                      <b>일출:</b> {current_weather['sunrise']}<br>
                      <b>일몰:</b> {current_weather['sunset']}<br>
                      <b>라운딩 적합도:</b> <span style="color:{playability_color};font-weight:bold;">{playability}</span>
                    </td>
                  </tr>
                </table>
              </td>
            </tr>"""

        # 5일 예보
        if forecast:
            forecast_rows = ""
            for day in forecast:
                rain_color = "#c62828" if day["rain_prob"] > 50 else "#f9a825" if day["rain_prob"] > 30 else "#2e7d32"
                forecast_rows += f"""
                  <td align="center" style="padding:6px 4px;font-size:12px;border-right:1px solid #f0f0f0;line-height:1.4;">
                    <b>{day['date']} ({day['weekday']})</b><br>
                    <span style="font-size:18px;">{day['icon']}</span><br>
                    {day['high']}° / {day['low']}°<br>
                    <span style="color:{rain_color};">강수 {day['rain_prob']}%</span><br>
                    바람 {day['wind']}m/s
                  </td>"""

            weather_section += f"""
            <tr>
              <td style="padding:0 14px 10px;">
                <table width="100%" cellpadding="0" cellspacing="0" border="0" style="border-top:1px solid #e8f5e9;">
                  <tr>
                    <td style="padding:8px 0 2px;font-size:14px;font-weight:bold;color:#555;">📅 주간 예보</td>
                  </tr>
                  <tr>{forecast_rows}</tr>
                </table>
              </td>
            </tr>"""

        weather_section += """
          </table>
        </td>
      </tr>
    </table>"""

    # ── 핵심 포인트 섹션 ──
    analysis_section = ""
    if analysis:
        analysis_section = f"""
    <table width="700" cellpadding="0" cellspacing="0" border="0" align="center" style="margin-bottom:10px;border-radius:8px;overflow:hidden;">
      <tr>
        <td bgcolor="#f0f7f2" style="padding:0;">
          <table width="100%" cellpadding="0" cellspacing="0" border="0">
            <tr>
              <td style="padding:10px 14px;font-size:17px;font-weight:bold;color:#1a5632;border-bottom:2px solid #c8e6c9;">
                📋 오늘의 핵심 포인트
              </td>
            </tr>
            <tr>
              <td style="padding:10px 14px;font-size:14px;line-height:1.55;">
                <ul style="margin:0;padding-left:20px;">
                  {analysis}
                </ul>
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>"""

    # ── 뉴스 섹션들 ──
    icon_map = {
        "industry": "🏌️",
        "tournament": "🏆",
        "equipment": "⛳",
        "policy": "📜",
        "global": "🌍",
    }
    news_sections = ""
    for cat_key in ["industry", "tournament", "equipment", "policy", "global"]:
        if cat_key in news and news[cat_key]:
            label = categories_config.get(cat_key, {}).get("label", cat_key)
            icon = icon_map.get(cat_key, "📰")
            is_global = (cat_key == "global")
            news_sections += _build_news_section(label, icon, news[cat_key], is_global=is_global)

    # ── 자사 뉴스 섹션 ──
    self_section = ""
    if self_news:
        self_section = _build_news_section(
            f"{golf['name']} 관련 뉴스", "🔔", self_news, bg_color="#fff8e1"
        )

    # ── 전체 HTML 조합 ──
    html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{golf['name']} 골프 모닝브리핑 - {date_str}</title>
</head>
<body style="margin:0;padding:0;background-color:#f5f5f0;font-family:'Malgun Gothic','맑은 고딕',sans-serif;">

  <!-- 헤더 -->
  <table width="700" cellpadding="0" cellspacing="0" border="0" align="center" style="margin-top:12px;border-radius:8px 8px 0 0;overflow:hidden;">
    <tr>
      <td bgcolor="#1a5632" style="padding:16px 20px;text-align:center;">
        <div style="font-size:22px;font-weight:bold;color:#ffffff;letter-spacing:1px;">
          ⛳ {golf['name']} 모닝브리핑
        </div>
        <div style="font-size:13px;color:#a5d6a7;margin-top:4px;">
          {date_str} ({weekday}) | {golf['location']}
        </div>
      </td>
    </tr>
  </table>

  <!-- 본문 -->
  <table width="700" cellpadding="0" cellspacing="0" border="0" align="center">
    <tr>
      <td style="padding:10px 0 4px;">
        {weather_section}
        {analysis_section}
        {news_sections}
        {self_section}
      </td>
    </tr>
  </table>

  <!-- 푸터 -->
  <table width="700" cellpadding="0" cellspacing="0" border="0" align="center" style="border-radius:0 0 8px 8px;overflow:hidden;margin-bottom:12px;">
    <tr>
      <td bgcolor="#1a5632" style="padding:10px 20px;text-align:center;">
        <div style="font-size:12px;color:#a5d6a7;">
          본 브리핑은 {golf['name']} 경영진을 위해 자동 생성되었습니다.
        </div>
        <div style="font-size:11px;color:#81c784;margin-top:2px;">
          Powered by {golf['name_en']} Briefing System
        </div>
      </td>
    </tr>
  </table>

</body>
</html>"""

    return html


# ──────────────────────────────────────────────
# 출력 & 발송
# ──────────────────────────────────────────────

def save_html(html_content):
    """HTML 파일 저장"""
    now = datetime.now(tz=KST)
    filename = f"브리핑_{now.strftime('%Y-%m-%d')}.html"
    filepath = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(html_content)

    print(f"[저장] {filename} 저장 완료")
    return filepath


def send_email(config, html_content):
    """SMTP를 통한 이메일 발송 (Outlook/Office 365 또는 Gmail 지원)"""
    email_from = config.get("email_from", "")
    email_to = config.get("email_to", "")
    password = config.get("email_password", "") or config.get("gmail_app_password", "")

    # SMTP 설정 (기본: Outlook/Office 365)
    smtp_host = config.get("smtp_host", "smtp.office365.com")
    smtp_port = int(config.get("smtp_port", 587))

    if not all([email_from, email_to, password]):
        missing = []
        if not email_from:
            missing.append("EMAIL_FROM")
        if not email_to:
            missing.append("EMAIL_TO")
        if not password:
            missing.append("EMAIL_PASSWORD")
        print(f"[이메일] 이메일 설정이 완료되지 않았습니다. 누락: {', '.join(missing)}")
        return False

    now = datetime.now(tz=KST)
    weekday = WEEKDAY_KR[now.weekday()]
    golf_name = config["golf_course"]["name"]
    subject = f"[{golf_name}] 골프 모닝브리핑 - {now.strftime('%Y.%m.%d')} ({weekday})"

    # 수신자 목록 (쉼표 구분)
    recipients = [addr.strip() for addr in email_to.split(",") if addr.strip()]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = email_from
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(html_content, "html", "utf-8"))

    try:
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls()
            server.login(email_from, password)
            server.sendmail(email_from, recipients, msg.as_string())
        print(f"[이메일] {len(recipients)}명에게 발송 완료")
        return True
    except Exception as e:
        print(f"[이메일] 발송 실패: {e}")
        return False


# ──────────────────────────────────────────────
# 메인
# ──────────────────────────────────────────────

def main():
    try:
        t_start = time.time()
        print("=" * 50)
        print("사우스스프링스 골프 모닝브리핑 생성 시작")
        print("=" * 50)

        # 1. 설정 로드
        print("\n[1/5] 설정 로드...")
        config = load_config()
        print(f"  골프장: {config['golf_course']['name']} ({config['golf_course']['location']})")

        # 2. 날씨 + 뉴스 수집 (모두 병렬)
        print("\n[2/5] 날씨 & 뉴스 수집 (병렬)...")
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=4) as executor:
            fut_current = executor.submit(get_current_weather, config)
            fut_forecast = executor.submit(get_weather_forecast, config)
            fut_news = executor.submit(collect_news, config)
            fut_self = executor.submit(collect_golf_course_news, config)

            current_weather = fut_current.result()
            forecast = fut_forecast.result()
            news = fut_news.result()
            self_news = fut_self.result()

        if current_weather:
            print(f"  날씨: {current_weather['temp']}°C, {current_weather['description']}")
        else:
            print("  날씨 정보를 가져올 수 없습니다. 뉴스만 표시합니다.")

        total_articles = sum(len(articles) for articles in news.values()) + len(self_news)
        print(f"  수집 완료: 총 {total_articles}건 ({time.time()-t0:.1f}s)")

        # 2-1. 기사 본문 병렬 수집 (요약 품질 향상을 위해)
        print("\n[2-1/5] 기사 본문 병렬 수집...")
        t0 = time.time()
        enrich_with_content(list(news.values()) + [self_news])
        print(f"  본문 수집 완료 ({time.time()-t0:.1f}s)")

        # 3. AI 처리 (중복 제거 + 요약을 단일 호출에 통합, 카테고리별 병렬)
        print("\n[3/5] AI 중복 제거 + 요약 (병렬)...")
        t0 = time.time()
        api_key = config.get("claude_api_key", "")
        categories_config = config.get("news_categories", {})

        if api_key and api_key != "YOUR_ANTHROPIC_API_KEY":
            tasks = []  # (key, articles, label, is_global)
            for cat_key, articles in news.items():
                if not articles:
                    continue
                label = categories_config.get(cat_key, {}).get("label", cat_key)
                is_global = (cat_key == "global")
                tasks.append((cat_key, articles, label, is_global))
            if self_news:
                tasks.append(("__self__", self_news, "자사 뉴스", False))

            if tasks:
                with ThreadPoolExecutor(max_workers=min(8, len(tasks))) as executor:
                    futures = {
                        executor.submit(process_articles, articles, api_key, label, is_global): key
                        for key, articles, label, is_global in tasks
                    }
                    for future in as_completed(futures):
                        key = futures[future]
                        try:
                            processed = future.result()
                        except Exception as e:
                            print(f"  [{key}] 처리 예외: {e}")
                            continue
                        if key == "__self__":
                            self_news = processed
                        else:
                            news[key] = processed
        else:
            print("  Claude API 키가 설정되지 않아 AI 처리를 건너뜁니다.")
        print(f"  AI 처리 완료 ({time.time()-t0:.1f}s)")

        # 핵심 포인트 (요약 반영됨)
        print("\n  핵심 포인트 생성 중...")
        analysis = generate_analysis(config, current_weather, news, self_news)
        if analysis:
            print("  핵심 포인트 생성 완료")

        # 4. HTML 생성 & 저장
        print("\n[4/5] HTML 브리핑 생성...")
        html = generate_briefing(config, current_weather, forecast, news, self_news, analysis)
        filepath = save_html(html)

        # 5. 이메일 발송
        print("\n[5/5] 이메일 발송...")
        send_email(config, html)

        print("\n" + "=" * 50)
        print(f"브리핑 생성 완료! (총 {time.time()-t_start:.1f}s)")
        print("=" * 50)

    except Exception as e:
        print(f"\n[오류] 브리핑 생성 실패: {e}")
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
