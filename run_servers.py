import subprocess
import time

print("Starting HTTP Server (Port 8080) for Public Receipts (No SSL warnings)...")
http_proc = subprocess.Popen(["py", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"])

print("Starting HTTPS Server (Port 8000) for Secure Admin/Clerk Access...")
https_proc = subprocess.Popen(["py", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--ssl-keyfile", "server.key", "--ssl-certfile", "server.crt"])

try:
    http_proc.wait()
    https_proc.wait()
except KeyboardInterrupt:
    print("Shutting down servers...")
    http_proc.terminate()
    https_proc.terminate()
