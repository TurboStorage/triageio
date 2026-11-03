import sys
import os
import ctypes

base_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.append(base_dir)

if os.name == "nt":
    try:
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)
    except Exception as e:
        print(f"[!] Could not hide console window: {e}", file=sys.stderr)

from triageio.desktop_app import launch_gui

if __name__ == "__main__":
    launch_gui()
