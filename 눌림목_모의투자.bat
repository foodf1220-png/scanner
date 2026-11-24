@echo off
chcp 65001 > nul
cd /d "%~dp0"
echo 눌림목 C자리 -^> B자리 모의매매 (설정: pullback_config.json, 중지: Ctrl+C)
echo 정규장 시작(한국 23:30, 서머타임 22:30) 전에 켜 두면 첫 5분봉 마감부터 감시합니다.
"%LOCALAPPDATA%\Programs\Python\Python312\python.exe" pullback.py --paper
pause
