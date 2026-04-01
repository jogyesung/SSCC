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
    if os.environ.get("GMAIL_APP_PASSWORD"):
        config["gmail_app_password"] = os.environ["GMAIL_APP_PASSWORD"]
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
    config.setdefault("max_age_days", 3)
    config.setdefault("blocked_domains", ["pinterest.com", "youtube.com"])

    return config


def _gn(query, hl="ko", gl="KR"):
    """Google News RSS URL 생성"""
    return f"https://news.google.com/rss/search?q={quote(query)}&hl={hl}&gl={gl}&ceid={gl}:{hl}"


def _is_korean(text):
    """텍스트에 한국어가 포함되어 있는지 확인"""
    return bool(re.search(r"[\uac00-\ud7a3]", text))


def _normalize_url(url):
    """URL 정규화 (추적 파라미터 제거)"""
    parsed = urlparse(url)
    # Google News 리다이렉트 URL에서 실제 URL 추출
    if "news.google.com" in parsed.netloc:
        return url  # Google News URL은 그대로 사용 (리다이렉트)
    # 일반 URL은 쿼리 파라미터 제거
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


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

            # 소스 추출
            source = ""
            if hasattr(entry, "source") and hasattr(entry.source, "title"):
                source = entry.source.title
            elif domain:
                source = domain.replace("www.", "")

            articles.append({
                "title": entry.get("title", "제목 없음"),
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
    """카테고리별 뉴스 수집 + 중복 제거"""
    categories = config.get("news_categories", {})
    max_articles = config.get("max_articles_per_category", 8)
    max_age = config.get("max_age_days", 3)
    blocked = config.get("blocked_domains", [])

    all_news = {}
    seen_urls = set()

    for cat_key, cat_config in categories.items():
        label = cat_config.get("label", cat_key)
        lang = cat_config.get("lang", "ko")

        # 해외 뉴스는 영문 키워드만, 국내 뉴스는 한국어+영문 키워드
        if lang == "en":
            keywords = cat_config.get("keywords_en", [])
        else:
            keywords = cat_config.get("keywords_kr", []) + cat_config.get("keywords_en", [])

        print(f"  [{label}] 뉴스 수집 중...")
        cat_articles = []

        for keyword in keywords:
            if lang == "en":
                url = _gn(keyword, hl="en", gl="US")
            else:
                url = _gn(keyword)
            articles = fetch_rss(url, limit=max_articles, max_age_days=max_age, blocked_domains=blocked)

            for article in articles:
                normalized = _normalize_url(article["link"])
                if normalized not in seen_urls:
                    seen_urls.add(normalized)
                    cat_articles.append(article)

            time.sleep(0.5)  # Google News 요청 간격

        # 카테고리당 최대 기사 수 제한
        all_news[cat_key] = cat_articles[:max_articles]
        print(f"  [{label}] {len(all_news[cat_key])}건 수집 완료")

    return all_news


def collect_golf_course_news(config):
    """자사(사우스스프링스) 관련 뉴스 모니터링"""
    golf = config["golf_course"]
    max_age = config.get("max_age_days", 3)
    blocked = config.get("blocked_domains", [])

    keywords = [golf["name"], f"{golf['location']} 골프장"]
    articles = []
    seen_urls = set()

    print(f"  [자사 뉴스] {golf['name']} 관련 뉴스 수집 중...")

    for keyword in keywords:
        url = _gn(keyword)
        results = fetch_rss(url, limit=5, max_age_days=max_age, blocked_domains=blocked)
        for article in results:
            normalized = _normalize_url(article["link"])
            if normalized not in seen_urls:
                seen_urls.add(normalized)
                articles.append(article)
        time.sleep(0.5)

    print(f"  [자사 뉴스] {len(articles)}건 수집 완료")
    return articles[:5]


# ──────────────────────────────────────────────
# AI 처리 (Claude)
# ──────────────────────────────────────────────

def translate_titles(titles, api_key):
    """영문 제목을 한국어로 번역 (Claude Haiku)"""
    if not titles or not api_key:
        return {}

    # 한국어가 아닌 제목만 필터
    to_translate = {i: t for i, t in enumerate(titles) if not _is_korean(t)}
    if not to_translate:
        return {}

    numbered = "\n".join(f"{i+1}. {t}" for i, t in to_translate.items())
    prompt = f"""다음 영문 골프 뉴스 제목들을 자연스러운 한국어로 번역해주세요.
번역만 출력하고 번호를 유지해주세요.

{numbered}"""

    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        result_text = response.content[0].text

        translations = {}
        for line in result_text.strip().split("\n"):
            line = line.strip()
            match = re.match(r"(\d+)\.\s*(.+)", line)
            if match:
                idx = int(match.group(1)) - 1
                translations[idx] = match.group(2)

        return translations
    except Exception as e:
        print(f"[AI] 번역 실패: {e}")
        return {}


def summarize_global_articles(articles, api_key):
    """해외 골프 뉴스 영문 기사를 국문 제목+요약으로 변환 (Claude Haiku)"""
    if not articles or not api_key:
        return articles

    numbered = "\n".join(f"{i+1}. {a['title']}" for i, a in enumerate(articles))
    prompt = f"""다음은 해외 골프 뉴스 영문 기사 제목들입니다.
각 기사에 대해 아래 형식으로 출력해주세요:

번호. [한국어 제목 번역]
요약: [기사 제목에서 유추할 수 있는 핵심 내용을 한국어 1-2문장으로 요약]

{numbered}"""

    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        result_text = response.content[0].text.strip()

        # 파싱: "번호. 제목\n요약: ..." 형식
        current_idx = None
        for line in result_text.split("\n"):
            line = line.strip()
            if not line:
                continue

            title_match = re.match(r"(\d+)\.\s*\[?(.+?)\]?\s*$", line)
            summary_match = re.match(r"요약:\s*(.+)", line)

            if title_match:
                current_idx = int(title_match.group(1)) - 1
                if 0 <= current_idx < len(articles):
                    articles[current_idx]["title_kr"] = title_match.group(2).strip("[]")
            elif summary_match and current_idx is not None and 0 <= current_idx < len(articles):
                articles[current_idx]["summary_kr"] = summary_match.group(1)

        return articles
    except Exception as e:
        print(f"[AI] 해외 뉴스 요약 실패: {e}")
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

    # 카테고리별 제목 정리
    categories_config = config.get("news_categories", {})
    sections = []
    for cat_key, articles in news.items():
        label = categories_config.get(cat_key, {}).get("label", cat_key)
        titles = [a["title"] for a in articles[:5]]
        if titles:
            sections.append(f"[{label}]\n" + "\n".join(f"- {t}" for t in titles))

    news_text = "\n\n".join(sections) if sections else "수집된 뉴스 없음"

    # 자사 뉴스
    self_text = ""
    if self_news:
        self_text = "\n[자사 관련 뉴스]\n" + "\n".join(f"- {a['title']}" for a in self_news)

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
        client = anthropic.Anthropic(api_key=api_key)
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

def _build_news_section(title, icon, articles, bg_color="#ffffff", show_summary=False):
    """뉴스 카테고리 HTML 섹션 생성"""
    if not articles:
        return ""

    rows = ""
    for article in articles:
        source_badge = f'<span style="color:#888;font-size:12px;">{article["source"]}</span>' if article["source"] else ""
        date_badge = f'<span style="color:#aaa;font-size:11px;margin-left:8px;">{article["published"]}</span>' if article["published"] else ""

        # 해외 뉴스: 한국어 제목 + 요약 표시
        display_title = article.get("title_kr", article["title"]) if show_summary else article["title"]
        summary_html = ""
        if show_summary and article.get("summary_kr"):
            summary_html = f'<div style="color:#555;font-size:13px;margin-top:4px;padding:6px 10px;background:#f8f9fa;border-left:3px solid #1a5632;border-radius:2px;">{article["summary_kr"]}</div>'
        original_title = ""
        if show_summary and article.get("title_kr"):
            original_title = f'<div style="color:#999;font-size:11px;margin-top:2px;">{article["title"]}</div>'

        rows += f"""
        <tr>
          <td style="padding:8px 16px;border-bottom:1px solid #f0f0f0;font-size:14px;line-height:1.6;">
            <a href="{article['link']}" style="color:#1a1a1a;text-decoration:none;" target="_blank">{display_title}</a>
            {original_title}
            {summary_html}
            <br>{source_badge}{date_badge}
          </td>
        </tr>"""

    return f"""
    <table width="700" cellpadding="0" cellspacing="0" border="0" align="center" style="margin-bottom:16px;border-radius:8px;overflow:hidden;">
      <tr>
        <td bgcolor="{bg_color}" style="padding:0;">
          <table width="100%" cellpadding="0" cellspacing="0" border="0">
            <tr>
              <td style="padding:16px;font-size:18px;font-weight:bold;color:#1a5632;border-bottom:2px solid #e8f5e9;">
                {icon} {title}
                <span style="font-size:13px;color:#888;font-weight:normal;margin-left:8px;">{len(articles)}건</span>
              </td>
            </tr>
            {rows}
          </table>
        </td>
      </tr>
    </table>"""


def generate_briefing(config, current_weather, forecast, news, self_news, analysis, translations):
    """전체 HTML 브리핑 생성"""
    now = datetime.now(tz=KST)
    date_str = now.strftime("%Y년 %m월 %d일")
    weekday = WEEKDAY_KR[now.weekday()]
    golf = config["golf_course"]
    categories_config = config.get("news_categories", {})

    # 번역 적용
    all_articles = []
    for articles in news.values():
        all_articles.extend(articles)
    for idx, translated in translations.items():
        if idx < len(all_articles):
            all_articles[idx]["title"] = translated

    # ── 날씨 섹션 ──
    weather_section = ""
    if current_weather:
        playability = format_golf_weather(current_weather, forecast)
        playability_color = {
            "최적": "#2e7d32", "양호": "#558b2f",
            "보통": "#f9a825", "부적합": "#c62828",
        }.get(playability, "#666")

        weather_section = f"""
    <table width="700" cellpadding="0" cellspacing="0" border="0" align="center" style="margin-bottom:16px;border-radius:8px;overflow:hidden;">
      <tr>
        <td bgcolor="#ffffff" style="padding:0;">
          <table width="100%" cellpadding="0" cellspacing="0" border="0">
            <tr>
              <td style="padding:16px;font-size:18px;font-weight:bold;color:#1a5632;border-bottom:2px solid #e8f5e9;">
                🌤️ 오늘의 날씨 — {golf['location']}
              </td>
            </tr>
            <tr>
              <td style="padding:16px;">
                <table width="100%" cellpadding="8" cellspacing="0" border="0">
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
                  <td align="center" style="padding:8px;font-size:13px;border-right:1px solid #f0f0f0;">
                    <b>{day['date']} ({day['weekday']})</b><br>
                    <span style="font-size:20px;">{day['icon']}</span><br>
                    {day['high']}° / {day['low']}°<br>
                    <span style="color:{rain_color};">강수 {day['rain_prob']}%</span><br>
                    바람 {day['wind']}m/s
                  </td>"""

            weather_section += f"""
            <tr>
              <td style="padding:0 16px 16px;">
                <table width="100%" cellpadding="0" cellspacing="0" border="0" style="border-top:1px solid #e8f5e9;">
                  <tr>
                    <td style="padding:12px 0 4px;font-size:14px;font-weight:bold;color:#555;">📅 주간 예보</td>
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
    <table width="700" cellpadding="0" cellspacing="0" border="0" align="center" style="margin-bottom:16px;border-radius:8px;overflow:hidden;">
      <tr>
        <td bgcolor="#f0f7f2" style="padding:0;">
          <table width="100%" cellpadding="0" cellspacing="0" border="0">
            <tr>
              <td style="padding:16px;font-size:18px;font-weight:bold;color:#1a5632;border-bottom:2px solid #c8e6c9;">
                📋 오늘의 핵심 포인트
              </td>
            </tr>
            <tr>
              <td style="padding:16px;font-size:14px;line-height:1.8;">
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
            news_sections += _build_news_section(label, icon, news[cat_key], show_summary=is_global)

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
  <table width="700" cellpadding="0" cellspacing="0" border="0" align="center" style="margin-top:20px;border-radius:8px 8px 0 0;overflow:hidden;">
    <tr>
      <td bgcolor="#1a5632" style="padding:24px 20px;text-align:center;">
        <div style="font-size:24px;font-weight:bold;color:#ffffff;letter-spacing:1px;">
          ⛳ {golf['name']} 모닝브리핑
        </div>
        <div style="font-size:14px;color:#a5d6a7;margin-top:8px;">
          {date_str} ({weekday}) | {golf['location']}
        </div>
      </td>
    </tr>
  </table>

  <!-- 본문 -->
  <table width="700" cellpadding="0" cellspacing="0" border="0" align="center">
    <tr>
      <td style="padding:16px 0;">
        {weather_section}
        {analysis_section}
        {news_sections}
        {self_section}
      </td>
    </tr>
  </table>

  <!-- 푸터 -->
  <table width="700" cellpadding="0" cellspacing="0" border="0" align="center" style="border-radius:0 0 8px 8px;overflow:hidden;margin-bottom:20px;">
    <tr>
      <td bgcolor="#1a5632" style="padding:16px 20px;text-align:center;">
        <div style="font-size:12px;color:#a5d6a7;">
          본 브리핑은 {golf['name']} 경영진을 위해 자동 생성되었습니다.
        </div>
        <div style="font-size:11px;color:#81c784;margin-top:4px;">
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
    """Gmail SMTP를 통한 이메일 발송"""
    email_from = config.get("email_from", "")
    email_to = config.get("email_to", "")
    password = config.get("gmail_app_password", "")

    if not all([email_from, email_to, password]):
        print("[이메일] 이메일 설정이 완료되지 않았습니다. 발송을 건너뜁니다.")
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
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
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
        print("=" * 50)
        print("사우스스프링스 골프 모닝브리핑 생성 시작")
        print("=" * 50)

        # 1. 설정 로드
        print("\n[1/6] 설정 로드...")
        config = load_config()
        print(f"  골프장: {config['golf_course']['name']} ({config['golf_course']['location']})")

        # 2. 날씨 수집
        print("\n[2/6] 날씨 정보 수집...")
        current_weather = get_current_weather(config)
        forecast = get_weather_forecast(config)
        if current_weather:
            print(f"  현재: {current_weather['temp']}°C, {current_weather['description']}")
        else:
            print("  날씨 정보를 가져올 수 없습니다. 뉴스만 표시합니다.")

        # 3. 뉴스 수집
        print("\n[3/6] 골프 뉴스 수집...")
        news = collect_news(config)
        self_news = collect_golf_course_news(config)

        total_articles = sum(len(articles) for articles in news.values()) + len(self_news)
        print(f"\n  총 {total_articles}건 수집 완료")

        # 4. AI 처리
        print("\n[4/6] AI 분석 처리...")
        api_key = config.get("claude_api_key", "")

        # 해외 뉴스 국문 요약
        if news.get("global"):
            print("  해외 뉴스 국문 요약 중...")
            news["global"] = summarize_global_articles(news["global"], api_key)
            summarized = sum(1 for a in news["global"] if a.get("summary_kr"))
            print(f"  해외 뉴스 {summarized}건 요약 완료")

        # 번역 (해외 뉴스 제외 - 이미 요약 처리됨)
        all_titles = []
        for cat_key, articles in news.items():
            if cat_key != "global":
                all_titles.extend(a["title"] for a in articles)
        translations = translate_titles(all_titles, api_key)
        if translations:
            print(f"  {len(translations)}건 번역 완료")

        # 분석
        analysis = generate_analysis(config, current_weather, news, self_news)
        if analysis:
            print("  핵심 포인트 생성 완료")

        # 5. HTML 생성
        print("\n[5/6] HTML 브리핑 생성...")
        html = generate_briefing(config, current_weather, forecast, news, self_news, analysis, translations)
        filepath = save_html(html)

        # 6. 이메일 발송
        print("\n[6/6] 이메일 발송...")
        send_email(config, html)

        print("\n" + "=" * 50)
        print("브리핑 생성 완료!")
        print("=" * 50)

    except Exception as e:
        print(f"\n[오류] 브리핑 생성 실패: {e}")
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
