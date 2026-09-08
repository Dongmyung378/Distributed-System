# Distributed System Recovery

외부 서비스와 수업용 라이브러리가 사라져 실행할 수 없었던 두 분산 시스템 실습을 독립 실행형 프로젝트로 복구한 저장소입니다.

- **Talk**: 여러 TCP 클라이언트가 접속해 전체/개인 메시지를 교환하는 채팅 시스템
- **Hotel**: 호텔과 밴드의 공통 시간대를 조회하고 예약하는 Wedding Planner

두 프로젝트 모두 Windows, macOS, Linux에서 Python만으로 실행할 수 있습니다. Hotel 클라이언트가 사용하는 `requests` 외에는 별도 프레임워크가 필요하지 않습니다.

## 복구 내용

### Talk

- 누락된 비공개 수업 라이브러리 `ex2utils` 의존성 제거
- Python 표준 라이브러리의 `socketserver`로 멀티스레드 TCP 서버 재구현
- 사용자 등록, 전체 메시지, 개인 메시지, 사용자 목록, 정상 종료 프로토콜 보존
- 공유 사용자 상태에 잠금을 적용하고 메시지 전송을 `sendall` 방식으로 변경

### Hotel

- 종료된 학교 Hotel/Band API와 호환되는 로컬 HTTP API 구현
- SQLite를 이용한 예약 데이터 영속 저장
- 트랜잭션과 유일성 제약으로 동시 중복 예약 방지
- 사용자별 서비스당 최대 2개 예약 제한 구현
- API timeout, 제한적 재시도, 오류 응답 매핑 추가
- 오래된 캐시 및 중복 메뉴 코드 제거
- 호텔 예약 후 밴드 예약이 실패하면 호텔 예약을 해제하는 보상 트랜잭션 유지

## 프로젝트 구조

```text
Distributed-System/
├── Hotel/
│   ├── api.ini             # 로컬 API 및 클라이언트 설정
│   ├── booking.py          # Wedding Planner CLI
│   ├── exceptions.py       # API 오류 타입
│   ├── local_api.py        # Hotel/Band 로컬 HTTP API
│   └── reservationapi.py   # 재시도 기능이 있는 HTTP 클라이언트
├── Talk/
│   ├── commands.json       # 채팅 프로토콜 명세
│   ├── myclient.py         # TCP 채팅 클라이언트
│   └── myserver.py         # 멀티스레드 TCP 채팅 서버
├── tests/                  # 자동 통합 테스트
├── requirements.txt
└── README.md
```

## 요구 사항

- Python 3.10 이상
- 로컬에서 사용할 수 있는 TCP 포트 `8090` 및 HTTP 포트 `8081`

버전을 확인합니다.

```powershell
python --version
```

## 설치

저장소 루트에서 가상 환경을 만들고 의존성을 설치합니다.

### Windows PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

PowerShell 실행 정책 때문에 활성화가 막히면 가상 환경의 Python을 직접 사용할 수 있습니다.

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### macOS/Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

## Talk 사용법

### 1. 서버 실행

첫 번째 터미널에서 실행합니다.

```powershell
python .\Talk\myserver.py
```

기본 주소는 `127.0.0.1:8090`입니다. 다른 주소나 포트를 사용하려면 인수를 전달합니다.

```powershell
python .\Talk\myserver.py 0.0.0.0 9000
```

`0.0.0.0`으로 열면 같은 네트워크의 다른 장치가 접속할 수 있습니다. 운영체제 방화벽에서 해당 포트를 허용해야 할 수 있습니다.

### 2. 클라이언트 실행

두 번째 터미널에서 실행하고 화면 이름을 입력합니다.

```powershell
python .\Talk\myclient.py
```

다중 사용자 테스트를 하려면 세 번째 터미널에서도 같은 명령을 실행합니다.

### 3. 채팅 명령

| 명령 | 설명 | 예시 |
|---|---|---|
| `send_all <message>` | 등록된 모든 사용자에게 전송 | `send_all Hello everyone` |
| `send_to <user> <message>` | 특정 사용자에게 전송 | `send_to Bob Hello Bob` |
| `user_list` | 현재 등록된 사용자 목록 | `user_list` |
| `quit` | 서버 확인을 받은 뒤 종료 | `quit` |

화면 이름은 공백 없이 입력해야 하며 동시에 중복 등록할 수 없습니다.

## Hotel 사용법

Hotel은 API 서버와 Wedding Planner 클라이언트를 각각 실행해야 합니다.

### 1. 로컬 예약 API 실행

첫 번째 터미널에서 실행합니다.

```powershell
python .\Hotel\local_api.py
```

기본적으로 다음 리소스가 생성됩니다.

- API 주소: `http://127.0.0.1:8081`
- 호텔 슬롯: 1~100
- 밴드 슬롯: 1~100
- 데이터베이스: `Hotel/data/reservations.db`

데이터베이스는 자동 생성되며 서버를 다시 시작해도 예약이 유지됩니다.

처음 상태로 초기화하려면 서버를 종료한 후 실행합니다.

```powershell
python .\Hotel\local_api.py --reset-data
```

임시 포트, 데이터베이스, 슬롯 개수를 지정할 수도 있습니다.

```powershell
python .\Hotel\local_api.py --port 9001 --database .\Hotel\data\demo.db --slots 250
```

포트를 바꾸면 `Hotel/api.ini`의 Hotel/Band URL도 같은 포트로 변경해야 합니다.

### 2. Wedding Planner 실행

두 번째 터미널에서 실행합니다.

```powershell
python .\Hotel\booking.py
```

메뉴에서 다음 기능을 사용할 수 있습니다.

1. 현재 Hotel/Band 예약 조회
2. 예약 가능한 슬롯 조회
3. Hotel 또는 Band 수동 예약
4. 예약 취소
5. 두 서비스에 모두 가능한 슬롯 조회
6. 가장 빠른 공통 슬롯 자동 예약
7. 중복되거나 불필요한 예약 정리

가장 간단한 확인 방법은 메뉴 `6`을 선택한 뒤 메뉴 `1`을 선택하는 것입니다. 정상이라면 Hotel과 Band 모두 같은 슬롯 `1`이 표시됩니다.

### 로컬 사용자 토큰

`Hotel/api.ini`에는 실행 예제를 위한 개발용 토큰 두 개가 서비스별로 등록되어 있습니다. 이것들은 실제 비밀 키가 아닙니다.

클라이언트가 사용할 토큰은 다음 환경변수로 설정값을 덮어쓸 수 있습니다.

```powershell
$env:HOTEL_API_KEY = "hotel-demo-user-2"
$env:BAND_API_KEY = "band-demo-user-2"
python .\Hotel\booking.py
```

실제 서비스와 연결할 때는 비밀 키를 Git에 커밋하지 말고 환경변수를 사용하십시오.

## Hotel API 계약

모든 요청은 `Authorization: Bearer <token>` 헤더를 사용합니다.

| Method | Endpoint | 설명 |
|---|---|---|
| `GET` | `/{service}/api/reservation/available` | 예약 가능한 슬롯 조회 |
| `GET` | `/{service}/api/reservation` | 현재 토큰의 예약 조회 |
| `POST` | `/{service}/api/reservation/{id}` | 슬롯 예약 |
| `DELETE` | `/{service}/api/reservation/{id}` | 슬롯 취소 |

`service`는 `hotel` 또는 `band`입니다. 주요 오류 코드는 다음과 같습니다.

| 상태 코드 | 의미 |
|---|---|
| `400` | 잘못된 슬롯 형식 또는 요청 |
| `401` | 토큰 누락 또는 잘못된 토큰 |
| `403` | 존재하지 않는 슬롯 |
| `404` | 해당 사용자가 보유하지 않은 예약 |
| `409` | 다른 사용자가 이미 예약한 슬롯 |
| `451` | 서비스당 최대 2개 예약 제한 초과 |

## 자동 테스트

서버를 별도로 실행하지 않은 상태에서 저장소 루트에서 실행합니다. 테스트가 임시 포트와 임시 데이터베이스를 자동으로 사용합니다.

```powershell
python -m unittest discover -s tests -v
```

테스트 범위는 다음과 같습니다.

- Hotel/Band API 조회, 예약, 취소
- 잘못된 토큰과 예약 한도
- 두 사용자의 동시 슬롯 충돌
- Wedding Planner의 최적 공통 슬롯 예약
- Talk 사용자 등록, 전체/개인 메시지, 사용자 목록, 종료
- 미등록 사용자 및 잘못된 명령 처리

## 문제 해결

### `Address already in use` 또는 포트 사용 오류

기존 서버를 `Ctrl+C`로 종료하거나 다른 포트를 지정하십시오. Talk 클라이언트에는 서버와 같은 포트를 전달해야 합니다.

```powershell
python .\Talk\myserver.py 127.0.0.1 9000
python .\Talk\myclient.py 127.0.0.1 9000
```

### Hotel 클라이언트가 연결되지 않음

`local_api.py`가 먼저 실행 중인지 확인하고 `Hotel/api.ini`의 포트가 서버 출력과 같은지 확인하십시오.

### 예약 데이터를 완전히 지우고 싶음

실행 중인 API를 종료하고 `--reset-data` 옵션을 사용하십시오. 이 작업은 현재 설정된 SQLite 예약 데이터베이스를 새로 만듭니다.

## 데이터와 보안

- 생성되는 SQLite 데이터와 Python 캐시는 `.gitignore`로 제외됩니다.
- 기본 토큰은 로컬 데모 전용이며 인증 보안을 제공하지 않습니다.
- 공개 네트워크에 배포하려면 HTTPS, 안전한 토큰 저장, 요청 제한, 로그 정제가 추가로 필요합니다.
- 사라진 학교 서버의 과거 예약 이력은 포함하지 않으며 새 로컬 데이터로 시작합니다.
