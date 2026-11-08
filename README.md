# Abstract Review — 초록 제출·리뷰 할당·수거 시스템

Flask + PostgreSQL. 저자 제출(텍스트 + PDF) → 리뷰어 등록 → 자동/수동 할당(double-blind, 편당 k명)
→ 리뷰 수거 → 결정 → 일괄 통보.

## 역할
- **저자**: 누구나 가입 → 제출/수정/철회(마감 전) → 결정 공개 후 결과·리뷰 코멘트 확인
- **리뷰어**: 관리자가 CSV로 등록 → 초대 메일 링크로 비밀번호 설정 → 배정 초록 리뷰(점수 1–5, 확신도 1–3), 이해충돌 신고
- **관리자**: 환경변수 ADMIN_EMAIL/ADMIN_PASSWORD 계정이 서버 시작 시 자동 생성됨 (seed 불필요)

## 처음 배포 (한 번만)
```bash
unzip abstract-review.zip && cd abstract-review
git init && git add . && git commit -m "Initial abstract review system"
git branch -M main
git remote add origin https://github.com/<계정>/abstract-review.git   # GitHub에서 빈 repo 먼저 생성
git push -u origin main
```
Render → **New → Blueprint** → 이 repo 선택 → render.yaml 이 웹서비스 + Postgres 를 함께 만듭니다.
생성 화면에서 값 입력:
| 변수 | 예시 |
|---|---|
| ADMIN_EMAIL | 본인 이메일 |
| ADMIN_PASSWORD | 8자 이상 |
| BASE_URL | https://abstract-review.onrender.com (배포 후 실제 주소로 수정) |
| MAIL_FROM / MAIL_FROM_NAME | 발신 주소 / "XXX 2028 Program Committee" |
| BREVO_API_KEY | (선택) 메일 실제 발송용 |

배포 후 `BASE_URL/login` 에서 관리자 로그인 → **Admin → Settings** 에서 학회명·마감·토픽 목록 설정.

## 코드 업데이트 시
```bash
unzip -o abstract-review.zip -d .     # 기존 폴더 위에 덮어쓰기
cd abstract-review
git add . && git commit -m "Update" && git push
```
Render 대시보드 → abstract-review → **Manual Deploy → Deploy latest commit**. (seed 단계 없음 — 테이블·관리자 자동 생성)

## 메일 발송
모든 메일은 먼저 **Admin → Outbox** 에 저장되고, 아래 중 설정된 방식으로 발송됩니다.
1. `BREVO_API_KEY` (HTTP API, 권장): brevo.com 무료 계정 → 발신 이메일 인증 → API key 발급
2. `SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD` (예: Gmail 앱 비밀번호). Render 무료 플랜은 SMTP 포트가 막혀 있을 수 있음
3. 둘 다 없으면 발송 안 됨 → Outbox 에서 초대/비밀번호 링크를 복사해 직접 전달 가능

## 운영 순서
1. Settings: 마감(KST), 토픽 목록(한 줄에 하나), 단어 제한, PDF 크기
2. Reviewers: `name,email,affiliation,topic1;topic2` 붙여넣기 → 초대 메일
3. 제출 마감 후 Auto-assign (편당 2명). 기존 할당은 유지하고 빈 자리만 채움 — 이해충돌 신고가 들어오면 다시 누르면 그 자리만 재배정
4. 마감 전 Overview → 리마인더 발송
5. Submissions: 평균/불일치(±2 이상) 순 정렬 → 결정 일괄 저장
6. Overview → Release decisions (결정 메일 일괄 발송)

## 이해충돌 자동 제외 규칙
본인 제출 / 저자 이메일 일치 / 소속명 일치 / 기관 이메일 도메인 일치(gmail·naver 등 웹메일 제외).
그 밖의 관계(지도교수·공동연구자)는 리뷰어가 리뷰 화면에서 직접 신고.

## 저장 용량
PDF는 DB에 저장합니다(Render 디스크는 재배포 시 지워지므로). 200편 × 최대 5MB = 최대 1GB.
실제 초록 PDF는 보통 0.2–1MB라 수백 MB 수준이지만, 무료 Postgres 용량·만료 조건을 확인하고 운영 기간에는 유료 플랜을 권장합니다.
