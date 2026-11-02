import subprocess
import sys
import os
import socket
import signal
import re

def detect_lan_ip():
    """Dynamically detects the primary active local network IP address, strictly prioritizing wired Ethernet over Wi-Fi."""
    ethernet_ips = []
    wifi_ips = []
    other_ips = []

    # 1. Hardware-level inspection via Windows ipconfig to prioritize wired Ethernet over Wi-Fi
    try:
        proc = subprocess.run(['ipconfig'], capture_output=True, text=True, timeout=3)
        current_adapter = ''
        for line in proc.stdout.splitlines():
            line_str = line.strip()
            if not line_str:
                continue
            if 'adapter' in line_str.lower() and ':' in line_str:
                current_adapter = line_str.lower()
            elif 'ipv4' in line_str.lower() and ':' in line_str:
                match = re.search(r':\s*([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)', line_str)
                if match:
                    ip = match.group(1).strip()
                    if not ip.startswith('127.') and not ip.startswith('169.254.'):
                        if 'ethernet' in current_adapter and 'bluetooth' not in current_adapter and 'virtual' not in current_adapter:
                            if ip not in ethernet_ips:
                                ethernet_ips.append(ip)
                        elif 'wireless' in current_adapter or 'wi-fi' in current_adapter or 'wifi' in current_adapter:
                            if ip not in wifi_ips:
                                wifi_ips.append(ip)
                        else:
                            if ip not in other_ips:
                                other_ips.append(ip)
    except Exception:
        pass

    if ethernet_ips:
        return ethernet_ips[0]

    # 2. Fallback to standard socket IP discovery if no Ethernet cable is active
    raw_ips = other_ips + wifi_ips
    try:
        hostname = socket.gethostname()
        for ip in socket.gethostbyname_ex(hostname)[2]:
            if not ip.startswith('127.') and not ip.startswith('169.254.') and ip not in raw_ips:
                raw_ips.append(ip)
    except Exception:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0)
        s.connect(('10.254.254.254', 1))
        ip = s.getsockname()[0]
        s.close()
        if ip and not ip.startswith('127.') and not ip.startswith('169.254.') and ip not in raw_ips:
            raw_ips.append(ip)
    except Exception:
        pass

    if not raw_ips:
        return "127.0.0.1"

    def ip_priority(ip_str):
        if ip_str.startswith('192.168.0.'):
            return 0
        if ip_str.startswith('192.168.55.'):
            return 1
        if ip_str.startswith('192.168.1.'):
            return 2
        if ip_str.startswith('192.168.'):
            return 3
        if ip_str.startswith('10.'):
            return 4
        if ip_str.startswith('172.'):
            return 5
        return 6

    sorted_ips = sorted(raw_ips, key=ip_priority)
    return sorted_ips[0]

def main():
    # Pre-flight check: ensure websockets library is installed for offline real-time sync
    try:
        import websockets
    except ImportError:
        print("[NOTICE] Installing offline WebSocket engine...")
        subprocess.run([sys.executable, "-m", "pip", "install", "websockets", "--quiet"])

    # Ensure SSL certificate files exist for HTTPS server
    if not os.path.exists("server.crt") or not os.path.exists("server.key"):
        print("[NOTICE] Generating local security certificates...")
        try:
            from werkzeug.serving import make_ssl_devcert
            make_ssl_devcert("./server", host=detect_lan_ip())
            print("[OK] Local SSL certificates generated.")
        except Exception as e:
            print(f"[WARN] Could not generate SSL devcert: {e}")

    lan_ip = detect_lan_ip()

    print("=" * 70)
    print("  BOOKLETLOG OFFLINE LAN APPLICATION SERVER")
    print("=" * 70)
    print(f"  • Primary LAN IP:               {lan_ip}")
    print(f"  • Local Workstation (HTTPS):     https://localhost:8000")
    print(f"  • Mobile / Scanner LAN (HTTPS):  https://{lan_ip}:8000")
    print(f"  • Mobile Public Receipts (HTTP): http://{lan_ip}:8080")
    print("=" * 70)

    python_exe = sys.executable

    # Launch HTTP server (Port 8080)
    http_proc = subprocess.Popen([
        python_exe, "-m", "uvicorn", "app.main:app",
        "--host", "0.0.0.0",
        "--port", "8080",
        "--reload"
    ])

    # Launch HTTPS server (Port 8000)
    https_proc = subprocess.Popen([
        python_exe, "-m", "uvicorn", "app.main:app",
        "--host", "0.0.0.0",
        "--port", "8000",
        "--ssl-keyfile", "server.key",
        "--ssl-certfile", "server.crt",
        "--reload"
    ])

    def shutdown():
        print("\nShutting down BOOKLETLOG servers...")
        for proc in (http_proc, https_proc):
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    try:
        http_proc.wait()
        https_proc.wait()
    except KeyboardInterrupt:
        shutdown()
    finally:
        shutdown()

if __name__ == '__main__':
    main()
