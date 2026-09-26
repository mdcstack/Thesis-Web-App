@echo off
title BOOKLETLOG Secure LAN Server
cd /d "%~dp0"
IF NOT EXIST server.crt (
    echo Generating local security certificates...
    py -c "from werkzeug.serving import make_ssl_devcert; make_ssl_devcert('./server', host='192.168.55.100')"
)
py run_servers.py
pause