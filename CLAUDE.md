# 사우스스프링스 골프 모닝브리핑 시스템

## 프로젝트 목적
사우스스프링스 골프장 경영진을 위한 자동 조간브리핑 시스템.
골프 산업 뉴스, 대회 소식, 장비/시장 동향, 정책/규제 변화 및 날씨 정보를 수집하여
HTML 리포트로 생성하고 이메일로 발송한다.

## 기술 스택
- Python 3.11
- anthropic SDK (Claude AI)
- feedparser (RSS 수집)
- requests (HTTP)
- beautifulsoup4 (HTML 파싱)
- GitHub Actions (자동화)

## 실행
```bash
pip install -r requirements.txt
python morning_briefing.py
```

## 환경변수 (GitHub Actions Secrets)
- `CLAUDE_API_KEY` — Anthropic API 키
- `GMAIL_APP_PASSWORD` — Gmail 앱 비밀번호
- `EMAIL_FROM` — 발신 이메일
- `EMAIL_TO` — 수신 이메일 (쉼표 구분)
- `WEATHER_API_KEY` — OpenWeatherMap API 키

## 코딩 규칙
- 에러 메시지는 반드시 한국어로 출력
- silent failure 금지 — 모든 except 블록에서 에러 내용 출력
- API 비용 최적화: 번역/분석은 Claude Haiku 사용
- HTML은 Python에서 직접 구성 (AI 의존도 최소화)
- 이메일 호환 HTML: table 레이아웃, inline style, bgcolor 속성 사용
