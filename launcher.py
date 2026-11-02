"""
BOOKLETLOG — One-Click Native Desktop Launcher
=============================================
Automates starting MySQL, running the offline background server daemon,
verifying server readiness, and opening Microsoft Edge / Chrome in
dedicated frameless App Mode with zero SSL warnings.
"""

import sys
import os
import time
import socket
import tempfile
import subprocess
import urllib.request
import ctypes

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

def show_alert(title, message, is_error=True):
    """Displays a native Windows alert message box."""
    try:
        icon_flag = 0x10 if is_error else 0x40 # MB_ICONERROR or MB_ICONINFORMATION
        ctypes.windll.user32.MessageBoxW(0, message, title, icon_flag | 0x0)
    except Exception:
        print(f"[{title}] {message}")

def is_port_in_use(port, host="127.0.0.1"):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0

def ensure_mysql_running():
    """Checks if MySQL port 3306 is open; if not, attempts to start MySQL80 service."""
    if is_port_in_use(3306):
        return True
    
    # Try starting MySQL80 Windows Service
    try:
        subprocess.run(["net", "start", "MySQL80"], capture_output=True, timeout=10)
    except Exception:
        pass
    
    # Wait up to 5 seconds for MySQL port 3306 to open
    start = time.time()
    while time.time() - start < 5:
        if is_port_in_use(3306):
            return True
        time.sleep(0.5)
        
    return is_port_in_use(3306)

def wait_for_server(url="http://127.0.0.1:8080/api/sync-version", timeout=18):
    start = time.time()
    while time.time() - start < timeout:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "BOOKLETLOG-Launcher"})
            with urllib.request.urlopen(req, timeout=1.5) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False

def find_app_browser():
    """Finds Microsoft Edge or Google Chrome for dedicated frameless App Mode."""
    candidates = []
    
    pfiles_x86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
    pfiles = os.environ.get("PROGRAMFILES", r"C:\Program Files")
    local_app = os.environ.get("LOCALAPPDATA", "")

    candidates.append(os.path.join(pfiles_x86, "Microsoft", "Edge", "Application", "msedge.exe"))
    candidates.append(os.path.join(pfiles, "Microsoft", "Edge", "Application", "msedge.exe"))
    candidates.append(os.path.join(pfiles, "Google", "Chrome", "Application", "chrome.exe"))
    candidates.append(os.path.join(pfiles_x86, "Google", "Chrome", "Application", "chrome.exe"))
    if local_app:
        candidates.append(os.path.join(local_app, "Google", "Chrome", "Application", "chrome.exe"))

    for path in candidates:
        if os.path.isfile(path):
            return path
    return None

def main():
    workspace_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(workspace_dir)

    target_url = "https://localhost:8000"
    log_path = os.path.join(workspace_dir, "server_output.log")

    # 1. Ensure MySQL is active
    if not ensure_mysql_running():
        show_alert(
            "MySQL Not Running",
            "Could not connect to MySQL Server (Port 3306).\n\n"
            "Please ensure the MySQL service (MySQL80) or MySQL Workbench is running, "
            "then launch BOOKLETLOG again."
        )
        return

    # 2. Check if the server is already active on port 8000
    if not is_port_in_use(8000):
        # Resolve python.exe (even if launched via pythonw.exe) for valid process piping
        python_dir = os.path.dirname(sys.executable)
        python_exe = os.path.join(python_dir, "python.exe")
        if not os.path.isfile(python_exe):
            python_exe = sys.executable

        run_servers_script = os.path.join(workspace_dir, "run_servers.py")

        # Open log file to capture background stdout/stderr
        try:
            log_file = open(log_path, "a", encoding="utf-8")
            log_file.write(f"\n{'='*60}\nBOOKLETLOG Background Daemon Started: {time.ctime()}\n{'='*60}\n")
            log_file.flush()
        except Exception:
            log_file = subprocess.DEVNULL

        subprocess.Popen(
            [python_exe, run_servers_script],
            cwd=workspace_dir,
            stdout=log_file,
            stderr=log_file,
            creationflags=CREATE_NO_WINDOW
        )

        # 3. Wait until HTTP server is responding
        server_ready = wait_for_server("http://127.0.0.1:8080/api/sync-version", timeout=18)
        if not server_ready:
            # Try reading the last lines of the log file to give the user a clear explanation
            recent_err = ""
            if os.path.isfile(log_path):
                try:
                    with open(log_path, "r", encoding="utf-8", errors="ignore") as lf:
                        lines = lf.readlines()
                        recent_err = "".join(lines[-10:])
                except Exception:
                    pass

            show_alert(
                "BOOKLETLOG Server Startup Timeout",
                "The server did not respond within 18 seconds.\n\n"
                f"Recent server output:\n{recent_err or 'No error log recorded.'}\n\n"
                "Please check server_output.log for more details."
            )
            return

    # 4. Locate browser and launch in frameless App Mode with SSL bypass
    browser_bin = find_app_browser()
    temp_profile = os.path.join(tempfile.gettempdir(), "bookletlog_desktop_profile")

    if browser_bin:
        browser_cmd = [
            browser_bin,
            f"--app={target_url}",
            "--ignore-certificate-errors",
            "--allow-insecure-localhost",
            f"--user-data-dir={temp_profile}",
            "--no-first-run",
            "--no-default-browser-check"
        ]
        # Launch browser without blocking server daemon
        subprocess.Popen(browser_cmd)
    else:
        import webbrowser
        webbrowser.open(target_url)

if __name__ == "__main__":
    main()
