import socket
import threading
import urllib.request
import webbrowser

from werkzeug.serving import make_server

from viewer_context import pystray


def detect_local_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "127.0.0.1"


def detect_public_ip():
    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=3) as resp:
            return resp.read().decode("utf-8").strip()
    except Exception:
        return "Unavailable"


def run_status_window(app, host, port, debug):
    import tkinter as tk
    from tkinter import ttk

    local_ip = detect_local_ip()
    public_ip = detect_public_ip()
    localhost_url = f"http://127.0.0.1:{port}"
    lan_url = f"http://{local_ip}:{port}"
    bind_label = "0.0.0.0 (all interfaces)" if host == "0.0.0.0" else host

    server = make_server(host, port, app)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    root = tk.Tk()
    root.title("Cosmos's Galery Manager - Server Status")
    root.geometry("560x310")
    root.resizable(False, False)
    root_mounted = True
    tray_icon = None

    wrap = ttk.Frame(root, padding=14)
    wrap.pack(fill="both", expand=True)

    ttk.Label(wrap, text="Cosmos's Galery Manager", font=("Segoe UI", 14, "bold")).pack(anchor="w")
    ttk.Label(wrap, text="Server is running").pack(anchor="w", pady=(2, 10))

    grid = ttk.Frame(wrap)
    grid.pack(fill="x", pady=(0, 12))

    def row(label, value):
        r = ttk.Frame(grid)
        r.pack(fill="x", pady=2)
        ttk.Label(r, text=label, width=14).pack(side="left")
        ttk.Label(r, text=value).pack(side="left")

    row("Bind Host:", bind_label)
    row("Port:", str(port))
    row("Local IP:", local_ip)
    row("Public IP:", public_ip)
    row("Local URL:", localhost_url)
    row("LAN URL:", lan_url)
    row("Debug:", str(debug))

    hint = (
        "Public access needs router port-forward + firewall allow rule.\n"
        "Use LAN URL for devices on the same network."
    )
    ttk.Label(wrap, text=hint).pack(anchor="w", pady=(0, 12))

    actions = ttk.Frame(wrap)
    actions.pack(fill="x")

    def open_local():
        webbrowser.open(localhost_url)

    def open_lan():
        webbrowser.open(lan_url)

    def show_window():
        if not root_mounted:
            return
        root.after(0, lambda: (root.deiconify(), root.lift(), root.focus_force()))

    def hide_window():
        if not root_mounted:
            return
        root.after(0, root.withdraw)

    def stop_from_tray(icon=None, item=None):
        stop_server()

    def setup_tray():
        nonlocal tray_icon
        if pystray is None:
            return
        try:
            from PIL import Image as PILImage, ImageDraw
        except Exception:
            return

        icon_img = PILImage.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(icon_img)
        d.ellipse((6, 6, 58, 58), fill=(11, 103, 255, 255), outline=(230, 240, 255, 255), width=2)
        d.rectangle((20, 18, 44, 46), fill=(240, 246, 255, 255))
        d.rectangle((24, 22, 40, 28), fill=(11, 103, 255, 255))
        d.rectangle((24, 32, 40, 42), fill=(11, 103, 255, 255))

        tray_icon = pystray.Icon(
            "cosmos_gallery_manager",
            icon_img,
            "Cosmos's Galery Manager",
            menu=pystray.Menu(
                pystray.MenuItem("Show Status", lambda icon, item: show_window()),
                pystray.MenuItem("Hide Status", lambda icon, item: hide_window()),
                pystray.MenuItem("Open Local", lambda icon, item: open_local()),
                pystray.MenuItem("Open LAN", lambda icon, item: open_lan()),
                pystray.MenuItem("Stop Server", stop_from_tray),
            ),
        )
        tray_icon.run_detached()

    def stop_server():
        nonlocal root_mounted
        if not root_mounted:
            return
        root_mounted = False
        try:
            if tray_icon is not None:
                try:
                    tray_icon.stop()
                except Exception:
                    pass
            server.shutdown()
        finally:
            root.destroy()

    ttk.Button(actions, text="Open Local", command=open_local).pack(side="left", padx=(0, 8))
    ttk.Button(actions, text="Open LAN", command=open_lan).pack(side="left", padx=(0, 8))
    ttk.Button(actions, text="Hide", command=hide_window).pack(side="left", padx=(0, 8))
    ttk.Button(actions, text="Stop Server", command=stop_server).pack(side="right")

    def on_close():
        if tray_icon is not None:
            hide_window()
        else:
            stop_server()

    root.protocol("WM_DELETE_WINDOW", on_close)
    setup_tray()
    root.mainloop()
