# Seokdam STT

석담교회 실시간 자막 및 번역 서비스입니다.

## 사용자 실행 및 자동 업데이트

배포 폴더에서 `Seokdam-Updater.exe`를 실행합니다. 업데이터는 GitHub Releases의
최신 버전을 확인하고, 새 버전이 있으면 SHA-256 검증 후 설치한 다음
`Seokdam-STT.exe`를 실행합니다. 인터넷 연결이나 업데이트 확인에 실패하면 기존
앱을 그대로 실행합니다.

API 키는 실행 파일과 같은 폴더의 `.env`에 보관되며 업데이트 시 수정하거나
삭제하지 않습니다.

```dotenv
CLOVA_SECRET=your_clova_secret
GEMINI_API_KEY=your_gemini_api_key
```

## 릴리스 만들기

로컬에서 `1.2.0` 릴리스 파일을 만들려면 다음 명령을 실행합니다.

```powershell
python -m pip install pyinstaller
powershell -NoProfile -ExecutionPolicy Bypass -File .\build_release.ps1 -Version 1.2.0
```

결과물은 `release/`에 생성됩니다. 운영 릴리스는 버전 태그를 push하면 GitHub
Actions가 같은 빌드를 실행하고 Release 자산을 게시합니다.

```powershell
git tag v1.2.0
git push origin v1.2.0
```

태그 버전은 `MAJOR.MINOR.PATCH` 형식이어야 합니다. 일반 앱 업데이트에는 고정
런처를 다시 배포할 필요가 없습니다. `version.json`의 `min_launcher_version`을
올린 경우에만 새 업데이터를 사용자에게 전달합니다.
석담 교회 자막 및 번역 서비스
