import subprocess
import sys

# Pre-flight check: ensure websockets library is installed for offline real-time sync
try:
    import websockets
    print("[OK] Offline LAN WebSocket protocol engine detected.")
except ImportError:
    print("[NOTICE] 'websockets' library not found. Installing offline WebSocket engine...")
    subprocess.run([sys.executable, "-m", "pip", "install", "websockets", "--quiet"])

print("Starting HTTP Server (Port 8080) for Public Receipts (http://192.168.55.100:8080)...")
http_proc = subprocess.Popen(["py", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--reload"])

print("Starting HTTPS Server (Port 8000) for Secure Admin/Clerk Access (https://192.168.55.100:8000)...")
https_proc = subprocess.Popen(["py", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--ssl-keyfile", "server.key", "--ssl-certfile", "server.crt", "--reload"])

try:
    http_proc.wait()
    https_proc.wait()
except KeyboardInterrupt:
    print("Shutting down servers...")
    http_proc.terminate()
    https_proc.terminate()
