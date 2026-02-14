@echo off
REM Kalshi Weather Scanner — Windows Task Scheduler wrapper
REM Schedule this .bat file via Task Scheduler to run at 7, 8, 9 AM ET.

setlocal

REM ── Working directory ──────────────────────────────────────────────
cd /d "%~dp0"

REM ── Ensure logs directory exists ───────────────────────────────────
if not exist "logs" mkdir logs

REM ── Date stamp for log file ────────────────────────────────────────
for /f "tokens=2 delims==" %%I in ('wmic os get localdatetime /value') do set dt=%%I
set LOGDATE=%dt:~0,4%-%dt:~4,2%-%dt:~6,2%_%dt:~8,2%%dt:~10,2%

REM ── Credentials ──────────────────────────────────────────────────────
set KALSHI_API_KEY_ID=c4201b27-6efa-4c51-8e96-b7a0f65d6ff2
set KALSHI_PRIVATE_KEY_PATH=C:\Users\Tyler\Desktop\Weather\Key\MIIEogIBAAKCAQEA0WDsnb6UnuhOvkYtzvx.pem
set GMAIL_ADDRESS=tylergwlk@gmail.com
set GMAIL_APP_PASSWORD=scdb knqs dsbk cwot
set EMAIL_TO=tylergwlk@gmail.com

REM ── Activate venv if present ───────────────────────────────────────
if exist ".venv\Scripts\activate.bat" (
    call .venv\Scripts\activate.bat
)

REM ── Run the scanner ────────────────────────────────────────────────
echo [%date% %time%] Starting Kalshi Weather Scanner >> "logs\%LOGDATE%.log"
python -m kalshi_weather >> "logs\%LOGDATE%.log" 2>&1
echo [%date% %time%] Scanner finished with exit code %ERRORLEVEL% >> "logs\%LOGDATE%.log"

endlocal
