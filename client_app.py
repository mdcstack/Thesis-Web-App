import webview

def on_loaded():
    pass

if __name__ == '__main__':
    # Creates a dedicated desktop window loading the LAN server
    window = webview.create_window(
        title='BOOKLETLOG Workstation Terminal',
        url='https://192.168.55.100:8000',
        width=1280,
        height=800,
        resizable=True,
        confirm_close=True
    )
    # Starts the window with SSL verification bypassed internally
    webview.start(private_mode=False, ssl=True)