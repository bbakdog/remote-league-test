# LoL 리모컨

폰으로 PC의 리그오브레전드 클라이언트를 조작합니다 (큐/수락/포지션/챔프 선택/스펠/룬).

```
LoLRemote/
├─ pc/        PC 프로그램 (LoLRemote.exe 로 빌드)
├─ android/   폰 앱 (APK 로 빌드)
└─ .github/   APK 자동 빌드 설정 (GitHub Actions)
```

## 1. PC용 exe 만들기 (윈도우, Python 설치돼 있어야 함)

1. `pc` 폴더 안의 `build_exe.bat` 을 더블클릭
2. 몇 분 뒤 `pc\dist\LoLRemote.exe` 가 생깁니다. 이 파일 하나만 있으면 돼요 (원하는 곳에 옮겨도 됨).

처음 실행하면 작업 표시줄 트레이(시계 옆)에 아이콘이 생기고, QR 창이 한 번 열립니다.
트레이 아이콘 메뉴: 폰 연결(QR 보기) / 윈도우 시작 시 자동 실행 / 연결 초기화 / 종료

> 서명 안 된 exe라서 윈도우 SmartScreen이 "PC 보호" 경고를 띄울 수 있어요.
> "추가 정보 → 실행"을 누르면 됩니다. 백신이 오탐하면 예외로 추가하세요.

## 2. 폰용 APK 만들기

### 방법 A. GitHub에서 자동 빌드 (설치 없이, 추천)
1. GitHub에 새 저장소(비공개 가능)를 만들고 이 폴더 전체를 올립니다 (`.github` 폴더 포함).
2. 저장소의 **Actions → Build APK → Run workflow**
3. 끝나면 실행 결과 페이지 아래 **Artifacts → LoLRemote-apk** 를 받아 압축을 풀면 `app-debug.apk`

### 방법 B. Android Studio
1. `android` 폴더를 Android Studio로 열기 (처음엔 다운로드/동기화에 시간이 걸려요)
2. **Build → Build Bundle(s) / APK(s) → Build APK(s)**
3. `android/app/build/outputs/apk/debug/app-debug.apk`

APK를 폰으로 옮겨 설치하세요 ("알 수 없는 앱 설치"를 허용해야 해요).

## 3. 연결하기

1. PC에서 `LoLRemote.exe` 실행 → QR 창이 열림
2. 폰에서 LoL 리모컨 앱 실행 → 카메라가 켜지면 PC의 QR 스캔
3. 끝. 다음부터는 앱만 열면 바로 연결돼요 (PC와 같은 와이파이, PC에서 exe 실행 중이어야 함)

## 문제가 생기면
- 연결 초기화: 트레이 메뉴 "연결 초기화" → 새 QR을 폰에서 다시 스캔
- 앱 안의 큐 탭 맨 아래 "PC 다시 연결"로도 QR을 다시 스캔할 수 있어요.
- 설정 파일 위치: `%APPDATA%\LoLRemote` (토큰 저장)
- 롤 클라이언트가 관리자 권한이면 LoLRemote.exe도 "관리자 권한으로 실행"
