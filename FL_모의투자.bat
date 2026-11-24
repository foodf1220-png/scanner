@echo off
chcp 65001 > nul
cd /d "%~dp0"
echo Fashionably Late V4.0 모의투자 (설정: fl_config.json, 중지: Ctrl+C)
echo 정규장 시작(한국 23:30, 서머타임 22:30) 전에 켜 두면 첫 1분봉 마감부터 감시합니다. 신규 매수는 뉴욕 10:00~13:30.
echo 감시 화면: logs\fl_monitor.html (잠시 뒤 브라우저로 열림, 10초마다 새로 고침)
if not exist logs mkdir logs
start "" /min cmd /c "timeout /t 90 > nul & start "" "%~dp0logs\fl_monitor.html""
"%LOCALAPPDATA%\Programs\Python\Python312\python.exe" fl.py --paper
pause
