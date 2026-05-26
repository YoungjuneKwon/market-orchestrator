# KIS Open API Fork 사용자 설정 가이드 (자동 청산/일별 추천)

이 문서는 공개 저장소를 포크(Fork)한 사용자가 `Auto Floor Sell`(강제 청산)과 `Daily Recommender`(일별 추천)를 바로 실행할 수 있도록, 한국투자증권 Open API 신청/키 발급부터 GitHub Actions 설정까지 한 번에 정리한 안내서입니다.

## 1) 먼저 알아둘 점

- 이 저장소는 민감값을 코드에 저장하지 않고, GitHub Secrets/Variables 또는 로컬 `.env`로 주입하는 구조입니다.
- 포크 저장소는 원본 저장소의 시크릿을 상속하지 않으므로, **포크한 본인 계정에서 시크릿/변수를 다시 등록**해야 합니다.
- 한국투자증권 Open API는 계정/권한/환경(실전/모의), 호출 유량, 네트워크 조건(IP 등)에 따라 호출 성공 여부가 달라질 수 있습니다.

## 2) KIS_API_KEY 등 주요 값은 어디서 얻나?

### 2.1 필수 값 목록

아래 4개는 필수입니다.

- `KIS_API_KEY`: Open API 앱키(App Key)
- `KIS_API_SECRET`: Open API 앱시크릿(App Secret)
- `KIS_CANO`: 계좌번호 앞 8자리
- `KIS_ACNT_PRDT_CD`: 계좌상품코드(예: `01` 종합)

### 2.2 발급/확인 절차(요약)

1. 한국투자증권 계좌 개설 및 Open API 사용 가능 상태 확인
2. KIS Developers 로그인 후 Open API 서비스 신청
3. 앱키/앱시크릿 발급(실전/모의 구분 확인)
4. 계좌번호 체계 확인(앞 8자리 + 뒤 2자리)
5. 저장소 설정에 값 반영

## 3) 웹 검색/레퍼런스 확인 결과

아래 페이지들은 실제 설정 시 반드시 참고해야 하는 공식 경로입니다.

- KIS Developers 메인: <https://apiportal.koreainvestment.com/>
- 서비스 이용안내: <https://apiportal.koreainvestment.com/about-howto>
- Open API 서비스 소개: <https://apiportal.koreainvestment.com/about-open-api>
- API 문서 요약/가이드 진입점: <https://apiportal.koreainvestment.com/apiservice-summary>
- API 가이드 문서 진입점: <https://apiportal.koreainvestment.com/apiservice-apiservice>
- 오류코드/FAQ: <https://apiportal.koreainvestment.com/faq-error-code>
- 공식 샘플 저장소: <https://github.com/koreainvestment/open-trading-api>

참고: 공식 샘플 저장소 README에도 Open API 신청/앱키 발급/계좌정보 구성 절차가 정리되어 있습니다.

## 4) 포크 저장소에서 GitHub Actions를 정상 동작시키는 설정

## 4.1 Actions 활성화

- 포크 저장소의 `Actions` 탭에서 워크플로우 실행을 허용합니다.

## 4.2 Repository Secrets 등록

`Settings > Secrets and variables > Actions > New repository secret`

- `KIS_API_KEY`
- `KIS_API_SECRET`
- `KIS_CANO`
- `KIS_ACNT_PRDT_CD`
- `KIS_ACCESS_TOKEN` (초기에는 비워도 됨, 첫 실행 후 갱신 가능)
- `KIS_TOKEN_STATE_TOKEN` (토큰 상태를 Secret/Variable에 다시 쓰기 위한 GitHub 토큰)

## 4.3 Repository Variable 등록

`Settings > Secrets and variables > Actions > Variables > New repository variable`

- `KIS_ACCESS_TOKEN_ISSUED_AT` (초기 빈 값 허용)

## 4.4 KIS_TOKEN_STATE_TOKEN 권한 권장

- 워크플로우가 `gh secret set`, `gh variable set`을 실행하므로, 해당 저장소의 Actions secrets/variables 수정 권한이 필요합니다.
- 최소 권한 원칙으로 발급하되, 실제로는 저장소 수준 시크릿/변수 쓰기가 가능해야 합니다.

## 5) 워크플로우가 값을 사용하는 방식

- 자동 청산: `.github/workflows/auto-floor-sell.yml`
- 일별 추천: `.github/workflows/daily-recommender.yml`

두 워크플로우 공통 처리:

1. Secrets/Variables로 `services/trading-bot/account.json` 생성
2. 캐시된 토큰(`KIS_ACCESS_TOKEN`, `KIS_ACCESS_TOKEN_ISSUED_AT`)을 실행 인자로 주입
3. 실행 결과로 신규 토큰이 발급되면 임시 JSON 생성
4. 가능하면(권한 허용 시) 해당 값을 다시 Secret/Variable에 저장

## 6) 로컬 실행(선택)

로컬에서는 `.env.example`을 복사해 `.env`를 만들고 아래 키를 채워 실행합니다.

- `KIS_API_KEY`
- `KIS_API_SECRET`
- `KIS_CANO`
- `KIS_ACNT_PRDT_CD`
- `KIS_ENV` (`vps` 또는 `prod`)

## 7) 자주 막히는 이슈 체크리스트

- 403/인증 실패: 앱키/시크릿, 계좌 연결 상태, 실전/모의 환경 매칭 확인
- 호출 제한: KIS 공지의 호출 유량/TPS 제한 확인
- 포크에서 비정상 종료: Secrets/Variables 누락 여부 확인
- 토큰 상태 저장 실패: `KIS_TOKEN_STATE_TOKEN` 권한 부족 여부 확인
- 네트워크 조건 이슈: 필요 시 self-hosted runner + 고정 IP 검토

## 8) 보안 원칙

- 앱키/시크릿/토큰은 코드, 커밋, 이슈 본문에 평문으로 남기지 않습니다.
- 출력이 필요하면 마스킹(`****`) 처리합니다.
- 포크 공개 저장소에서도 시크릿은 반드시 GitHub Secrets로만 관리합니다.

## 9) 책임 및 법적 고지

- 본 저장소의 코드는 지식 공유를 목적으로 공개된 오픈소스이며, 제공자·기여자·배포자는 사용자의 개별 투자목적 달성, 수익 실현, 손실 방지에 관한 법적 의무를 부담하지 않습니다.
- 본 저장소의 코드, 문서, 예시 설정은 정보 제공 및 기술 참고를 위한 것이며, 특정 투자행위에 대한 권유, 자문, 보장 또는 대리행위를 구성하지 않습니다.
- 사용자는 본 저장소를 자신의 판단과 책임 하에 사용하며, 코드의 실행 또는 미실행, 설정 오류, 네트워크/브로커/API 장애, 시장 변동 등으로 발생하는 **모든 손실과 이익은 전적으로 사용자 본인에게 귀속**됩니다.
- 제공자·기여자·배포자는 관련 법령이 허용하는 최대 범위에서 직접손해, 간접손해, 특별손해, 결과손해(일실이익 포함)에 대한 책임을 부담하지 않습니다.
- 본 고지는 대한민국 법령 체계(민법상 계약자유 및 자기책임 원칙, 상법상 자기계산·위험부담 원칙, 저작권법상 저작물 이용허락 범위 제한)를 전제로 하며, 사용자는 저장소를 복제, 포크, 실행, 배포하는 시점에 본 고지에 동의한 것으로 봅니다.
- 다만, 고의 또는 중대한 과실에 따른 책임까지 면제되는 것은 아니며, 관련 강행법규가 우선 적용됩니다.
- 본 섹션은 일반 안내이며 개별 사안에 대한 법률자문이 아닙니다. 필요한 경우 대한민국 변호사 등 전문가의 자문을 받으시기 바랍니다.
