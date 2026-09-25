@echo off
title BOOKLETLOG Secure LAN Server
cd /d "%~dp0"
IF NOT EXIST server.crt (
    echo Generating local security certificates...
    py -c "from werkzeug.serving import make_ssl_devcert; make_ssl_devcert('./server', host='192.168.55.100')"
)
py -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --ssl-keyfile server.key --ssl-certfile server.crt
pause