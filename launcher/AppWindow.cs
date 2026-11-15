// Comfy Planner app window
//
// A plain WinForms window that hosts one WebView2 (the Microsoft Edge WebView2 runtime that ships with
// Windows 11 and is installed with Edge on Windows 10) and shows the local UI in it. There is no browser
// chrome: no address bar, no tabs, no browser context menu, no download bar.
//
// What lives here:
//   - window setup: title, icon, dark title bar, size/position memory (data\window-state.txt)
//   - WebView2 setup: profile folder, settings, background colour
//   - behaviour: links to other sites open in the default browser, a small right-click menu,
//     F5 / Ctrl+R reload, a Save dialog for downloads, a plain "cannot reach the server" page
//
// Plain C# 5 (see Launcher.cs). The DPI awareness that keeps the window sharp is declared in app.manifest.

using System;
using System.Diagnostics;
using System.Drawing;
using System.Globalization;
using System.IO;
using System.Runtime.InteropServices;
using System.Threading.Tasks;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;

namespace ComfyPlanner
{
    internal sealed class AppWindow : Form
    {
        // The UI's page background (--bg in ui\src\tokens.css). The window and the WebView2 use it, so nothing
        // flashes white before the page has painted. The UI is dark only; keep this in sync with tokens.css.
        private static readonly Color PageColor = Color.FromArgb(0x0A, 0x0A, 0x0B);

        // Window size when there is no saved one, in 96-dpi pixels (scaled to the screen).
        private const int DefaultWidth = 1440;
        private const int DefaultHeight = 900;

        // The window is shown as soon as the first page has loaded; if that takes this long, show it anyway.
        private const int ShowAfterMs = 6000;

        // Right-click menu: the items kept in each situation. Everything else is removed.
        private static readonly string[] EditableItems = { "undo", "redo", "cut", "copy", "paste", "selectAll" };
        private static readonly string[] SelectionItems = { "copy" };
        private static readonly string[] ImageItems = { "copyImage", "saveImageAs" };

        private const int WM_SETICON = 0x0080;
        private const int WM_DPICHANGED = 0x02E0;
        private const int ICON_SMALL = 0;
        private const int SM_CXSMICON = 49;
        private const int SW_RESTORE = 9;
        private const int DWMWA_USE_IMMERSIVE_DARK_MODE = 20;       // Windows 10 2004 and later, Windows 11
        private const int DWMWA_USE_IMMERSIVE_DARK_MODE_OLD = 19;   // earlier Windows 10 builds

        [DllImport("user32.dll")]
        private static extern bool SetForegroundWindow(IntPtr hWnd);

        [DllImport("user32.dll")]
        private static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);

        [DllImport("user32.dll")]
        private static extern IntPtr SendMessage(IntPtr hWnd, int msg, IntPtr wParam, IntPtr lParam);

        [DllImport("user32.dll")]
        private static extern uint GetDpiForWindow(IntPtr hWnd);

        [DllImport("user32.dll")]
        private static extern int GetSystemMetricsForDpi(int index, uint dpi);

        [DllImport("dwmapi.dll")]
        private static extern int DwmSetWindowAttribute(IntPtr hWnd, int attribute, ref int value, int size);

        [StructLayout(LayoutKind.Sequential)]
        private struct RECT
        {
            public int Left;
            public int Top;
            public int Right;
            public int Bottom;
        }

        private readonly Uri _appUri;
        private readonly string _url;
        private readonly string _stateFile;
        private readonly string _profileDir;
        private readonly string _iconFile;
        private readonly Action<string> _log;
        private readonly System.Windows.Forms.Timer _showTimer;

        private CoreWebView2Controller _controller;
        private CoreWebView2 _web;
        private Icon _bigIcon;
        private Icon _smallIcon;
        private bool _ready;            // the first page has loaded (or we gave up waiting)
        private bool _wantShow;         // someone asked for the window before it was ready
        private bool _shownOnce;
        private bool _startMaximized;
        private bool _wasMaximized;     // maximized the last time it was not minimized
        private bool _placing;          // ignore DPI-change messages while we position the window ourselves
        private bool _quitting;
        private bool _canSuspend;       // the window has been shown once, so hiding it may lower the WebView2's memory
        private bool _lowMemory;        // hidden or minimized: the WebView2 is asked to use less memory (SetLowMemory)
        private bool _showingError;     // the "cannot reach the server" page is up
        private bool _allowDataPage;    // the next navigation is our own error page

        public AppWindow(string root, string url, Action<string> log)
        {
            _url = url;
            _appUri = new Uri(url);
            _log = log;
            _stateFile = Path.Combine(root, "data\\window-state.txt");
            _profileDir = Path.Combine(root, "data\\webview2-profile");
            string iconFile = Path.Combine(root, "launcher\\icon.ico");
            _iconFile = File.Exists(iconFile) ? iconFile : null;

            Text = "Comfy Planner";
            BackColor = PageColor;
            AutoScaleMode = AutoScaleMode.None;
            StartPosition = FormStartPosition.Manual;
            Icon = LoadBigIcon();

            _showTimer = new System.Windows.Forms.Timer();
            _showTimer.Interval = ShowAfterMs;
            _showTimer.Tick += delegate { MarkReady(); };

            // Create the (still hidden) window now: the screen's DPI is known, and the dark title bar and the
            // icons are in place before the user ever sees it.
            CreateHandle();
            PlaceWindow();
        }

        // ---------- public ----------

        // Create the WebView2 and start loading the UI. Returns null when it works, or a short reason when it
        // does not (typically: the WebView2 runtime is not installed). Must be called on the UI thread.
        public async Task<string> StartAsync()
        {
            try
            {
                Directory.CreateDirectory(_profileDir);
                CoreWebView2Environment environment = await CoreWebView2Environment.CreateAsync(null, _profileDir, null);
                if (IsDisposed)
                {
                    return null; // quit while the runtime was starting
                }

                CoreWebView2Controller controller = await environment.CreateCoreWebView2ControllerAsync(Handle);
                if (IsDisposed)
                {
                    controller.Close();
                    return null;
                }

                _controller = controller;
                _controller.DefaultBackgroundColor = PageColor;
                _controller.Bounds = ClientRectangle;
                _controller.IsVisible = Visible && WindowState != FormWindowState.Minimized;
                _controller.AcceleratorKeyPressed += OnAcceleratorKeyPressed;

                _web = _controller.CoreWebView2;
                ApplySettings(_web.Settings);
                _web.NavigationStarting += OnNavigationStarting;
                _web.NavigationCompleted += OnNavigationCompleted;
                _web.NewWindowRequested += OnNewWindowRequested;
                _web.ContextMenuRequested += OnContextMenuRequested;
                _web.DownloadStarting += OnDownloadStarting;

                _showTimer.Start();
                _web.Navigate(_url);
                return null;
            }
            catch (Exception ex)
            {
                _log("WebView2 could not start: " + ex);
                return ex.Message;
            }
        }

        // Show the window and bring it to the front (restoring it if it was minimized or hidden to the tray).
        // If the first page is still loading, it appears as soon as that page has loaded.
        public void Present()
        {
            _wantShow = true;
            if (_ready)
            {
                ShowNow();
            }
        }

        // Quit: remember the position and close the WebView2, so its browser processes end with us.
        public void Shutdown()
        {
            _quitting = true;
            SaveState();
            _showTimer.Stop();
            CloseWebView();
            Close();
        }

        // ---------- WebView2 setup ----------

        private static void ApplySettings(CoreWebView2Settings settings)
        {
            settings.IsStatusBarEnabled = false;                // no link preview bar at the bottom
            settings.AreBrowserAcceleratorKeysEnabled = false;  // no Ctrl+F, Ctrl+P, F12, Alt+Left... (F5 and Ctrl+R are added back)
            settings.IsZoomControlEnabled = false;              // no Ctrl+wheel / Ctrl+plus zoom (the UI does not use Ctrl+wheel)
            settings.IsPinchZoomEnabled = false;
            settings.IsSwipeNavigationEnabled = false;
            settings.IsGeneralAutofillEnabled = false;
            settings.IsPasswordAutosaveEnabled = false;
            settings.IsBuiltInErrorPageEnabled = false;         // a failed load shows our own plain page instead
            settings.AreDevToolsEnabled = Environment.GetEnvironmentVariable("COMFY_PLANNER_DEVTOOLS") == "1";
        }

        // ---------- navigation ----------

        private bool IsAppUrl(string url)
        {
            Uri uri;
            return Uri.TryCreate(url, UriKind.Absolute, out uri)
                && uri.Scheme == _appUri.Scheme
                && string.Equals(uri.Host, _appUri.Host, StringComparison.OrdinalIgnoreCase)
                && uri.Port == _appUri.Port;
        }

        // Anything that leaves the app (a normal link, target=_blank) goes to the default browser instead.
        private void OnNavigationStarting(object sender, CoreWebView2NavigationStartingEventArgs e)
        {
            if (_allowDataPage && e.Uri.StartsWith("data:", StringComparison.OrdinalIgnoreCase))
            {
                _allowDataPage = false;
                return;
            }
            if (IsAppUrl(e.Uri))
            {
                return;
            }
            e.Cancel = true;
            OpenExternal(e.Uri);
        }

        private void OnNewWindowRequested(object sender, CoreWebView2NewWindowRequestedEventArgs e)
        {
            e.Handled = true; // never create a second window
            OpenExternal(e.Uri);
        }

        // Only http and https are handed to the shell. Anything else (file:, ms-*:, javascript:...) is dropped.
        private void OpenExternal(string url)
        {
            Uri uri;
            if (!Uri.TryCreate(url, UriKind.Absolute, out uri)
                || (uri.Scheme != Uri.UriSchemeHttp && uri.Scheme != Uri.UriSchemeHttps))
            {
                _log("blocked a link that is not http or https (" + (uri != null ? uri.Scheme : "invalid") + ")");
                return;
            }

            try
            {
                ProcessStartInfo info = new ProcessStartInfo(uri.AbsoluteUri);
                info.UseShellExecute = true;
                Process.Start(info);
                _log("opened in the default browser: " + uri.AbsoluteUri);
            }
            catch (Exception ex)
            {
                _log("could not open the default browser: " + ex.Message);
            }
        }

        private void OnNavigationCompleted(object sender, CoreWebView2NavigationCompletedEventArgs e)
        {
            if (e.IsSuccess)
            {
                if (_web != null && IsAppUrl(_web.Source))
                {
                    _showingError = false;
                }
                MarkReady();
                return;
            }
            if (e.WebErrorStatus == CoreWebView2WebErrorStatus.OperationCanceled)
            {
                return; // replaced by another navigation (a reload, say)
            }

            _log("the page did not load: " + e.WebErrorStatus);
            ShowErrorPage();
            MarkReady();
        }

        // Shown when the server cannot be reached. It retries by itself every few seconds.
        private void ShowErrorPage()
        {
            if (_web == null)
            {
                return;
            }
            _showingError = true;
            _allowDataPage = true;
            string html =
                "<!doctype html><meta charset=\"utf-8\"><title>Comfy Planner</title>"
                + "<style>html,body{height:100%;margin:0;background:#0a0a0b;color:#8e8e96;font:14px/1.5 'Segoe UI',system-ui,sans-serif}"
                + "body{display:flex;align-items:center;justify-content:center;text-align:center}"
                + "h1{margin:0 0 6px;font-size:16px;font-weight:600;color:#ededef}p{margin:0}</style>"
                + "<div><h1>Comfy Planner cannot reach its server</h1>"
                + "<p>It may still be starting, or it has stopped. Trying again...</p></div>"
                + "<script>setTimeout(function(){location.replace('" + _url + "/')},3000)</script>";
            _web.NavigateToString(html);
        }

        // F5 and Ctrl+R reload, so a stuck page can be recovered. The other browser keys stay off.
        private void OnAcceleratorKeyPressed(object sender, CoreWebView2AcceleratorKeyPressedEventArgs e)
        {
            if (e.KeyEventKind != CoreWebView2KeyEventKind.KeyDown && e.KeyEventKind != CoreWebView2KeyEventKind.SystemKeyDown)
            {
                return;
            }
            bool ctrl = (Control.ModifierKeys & Keys.Control) == Keys.Control;
            if (e.VirtualKey == (uint)Keys.F5 || (ctrl && e.VirtualKey == (uint)Keys.R))
            {
                e.Handled = true;
                if (e.PhysicalKeyStatus.WasKeyDown == 0) // ignore key repeat
                {
                    _log("reload requested from the keyboard");
                    Reload();
                }
            }
        }

        private void Reload()
        {
            if (_web == null)
            {
                return;
            }
            if (_showingError || !IsAppUrl(_web.Source))
            {
                _web.Navigate(_url);
            }
            else
            {
                _web.Reload();
            }
        }

        // ---------- right-click menu and downloads ----------

        // The browser menu is replaced by a small one: edit commands in text fields, Copy on selected text,
        // the image items on pictures, nothing anywhere else.
        private void OnContextMenuRequested(object sender, CoreWebView2ContextMenuRequestedEventArgs e)
        {
            CoreWebView2ContextMenuTarget target = e.ContextMenuTarget;
            string[] keep;
            if (target.IsEditable)
            {
                keep = EditableItems;
            }
            else if (target.Kind == CoreWebView2ContextMenuTargetKind.Image)
            {
                keep = ImageItems;
            }
            else if (target.HasSelection)
            {
                keep = SelectionItems;
            }
            else
            {
                keep = new string[0];
            }

            System.Collections.Generic.IList<CoreWebView2ContextMenuItem> items = e.MenuItems;
            for (int i = items.Count - 1; i >= 0; i--)
            {
                if (items[i].Kind != CoreWebView2ContextMenuItemKind.Separator && Array.IndexOf(keep, items[i].Name) < 0)
                {
                    items.RemoveAt(i);
                }
            }

            // Drop separators that now sit at the ends or next to each other.
            for (int i = items.Count - 1; i >= 0; i--)
            {
                if (items[i].Kind != CoreWebView2ContextMenuItemKind.Separator)
                {
                    continue;
                }
                bool atEnd = i == 0 || i == items.Count - 1;
                if (atEnd || items[i - 1].Kind == CoreWebView2ContextMenuItemKind.Separator)
                {
                    items.RemoveAt(i);
                }
            }

            if (items.Count == 0)
            {
                e.Handled = true; // nothing to show: no menu at all
            }
        }

        // A download (for example "Save image as") asks where to save in a normal Save dialog; there is no
        // browser download bar. The app itself does not download files.
        private void OnDownloadStarting(object sender, CoreWebView2DownloadStartingEventArgs e)
        {
            e.Handled = true; // hides the browser's own download UI
            CoreWebView2Deferral deferral = e.GetDeferral();
            try
            {
                string name = Path.GetFileName(e.ResultFilePath);
                string extension = Path.GetExtension(name);
                using (SaveFileDialog dialog = new SaveFileDialog())
                {
                    dialog.FileName = name;
                    dialog.OverwritePrompt = true;
                    dialog.Filter = (extension.Length > 1 ? extension.Substring(1).ToUpperInvariant() + " file|*" + extension + "|" : "")
                        + "All files|*.*";
                    if (dialog.ShowDialog(Visible ? this : null) == DialogResult.OK)
                    {
                        e.ResultFilePath = dialog.FileName;
                    }
                    else
                    {
                        e.Cancel = true;
                    }
                }
            }
            catch (Exception ex)
            {
                _log("download dialog failed: " + ex.Message);
                e.Cancel = true;
            }
            finally
            {
                deferral.Complete();
            }
        }

        // ---------- showing, hiding, closing ----------

        private void MarkReady()
        {
            _showTimer.Stop();
            if (_ready)
            {
                return;
            }
            _ready = true;
            if (_wantShow)
            {
                ShowNow();
            }
        }

        private void ShowNow()
        {
            if (IsDisposed)
            {
                return;
            }
            if (!_shownOnce)
            {
                _shownOnce = true;
                if (_startMaximized)
                {
                    WindowState = FormWindowState.Maximized;
                }
            }
            if (!Visible)
            {
                Show();
            }
            _canSuspend = true; // from now on, hiding the window lowers the WebView2's memory (SetLowMemory)
            if (WindowState == FormWindowState.Minimized)
            {
                ShowWindow(Handle, SW_RESTORE); // back to normal or maximized, whichever it was
            }
            Activate();
            BringToFront();
            SetForegroundWindow(Handle);
        }

        // The X button (and Alt+F4, and a taskbar "Close window") hides the window to the tray; the server keeps
        // running. Only Quit, or Windows shutting down, really closes it.
        protected override void OnFormClosing(FormClosingEventArgs e)
        {
            SaveState();
            if (!_quitting && e.CloseReason != CloseReason.WindowsShutDown && e.CloseReason != CloseReason.ApplicationExitCall)
            {
                e.Cancel = true;
                Hide();
                _log("window hidden to the tray");
                return;
            }
            base.OnFormClosing(e);
        }

        protected override void OnVisibleChanged(EventArgs e)
        {
            base.OnVisibleChanged(e);
            UpdateWebView();
        }

        protected override void OnSizeChanged(EventArgs e)
        {
            base.OnSizeChanged(e);
            if (WindowState == FormWindowState.Maximized)
            {
                _wasMaximized = true;
            }
            else if (WindowState == FormWindowState.Normal)
            {
                _wasMaximized = false;
            }
            UpdateWebView();
        }

        protected override void OnMove(EventArgs e)
        {
            base.OnMove(e);
            if (_controller != null)
            {
                _controller.NotifyParentWindowPositionChanged();
            }
        }

        protected override void OnResizeEnd(EventArgs e)
        {
            base.OnResizeEnd(e); // also raised at the end of a move
            SaveState();
        }

        // Keyboard focus goes into the page whenever the window is activated, so typing (and F5) work right after
        // switching back. Done after Windows has finished activating the window, or it would take the focus back.
        protected override void OnActivated(EventArgs e)
        {
            base.OnActivated(e);
            BeginInvoke(new MethodInvoker(FocusWebView));
        }

        protected override void OnGotFocus(EventArgs e)
        {
            base.OnGotFocus(e);
            FocusWebView();
        }

        private void FocusWebView()
        {
            if (_controller != null && Visible && WindowState != FormWindowState.Minimized)
            {
                _controller.MoveFocus(CoreWebView2MoveFocusReason.Programmatic);
            }
        }

        private void UpdateWebView()
        {
            if (_controller == null)
            {
                return;
            }
            bool shown = Visible && WindowState != FormWindowState.Minimized;
            _controller.Bounds = ClientRectangle;
            _controller.IsVisible = shown;
            if (_canSuspend)
            {
                SetLowMemory(!shown); // before the first show the page is still loading: leave it alone
            }
        }

        // While the window is hidden to the tray (or minimized) the page keeps its state (text in a field, the open
        // tab...) but the WebView2 is told to use as little memory as it can: a low memory target, and suspended, which
        // pauses the page's scripts and timers. Showing the window undoes both; the page then carries on and
        // catches up (the live-event stream of the UI re-syncs by itself).
        private async void SetLowMemory(bool low)
        {
            CoreWebView2 web = _web;
            if (web == null || low == _lowMemory)
            {
                return;
            }
            _lowMemory = low;
            try
            {
                if (low)
                {
                    web.MemoryUsageTargetLevel = CoreWebView2MemoryUsageTargetLevel.Low;
                    bool suspended = await web.TrySuspendAsync(); // only works while the WebView2 is not visible
                    if (!_lowMemory && web.IsSuspended)
                    {
                        web.Resume(); // the window came back while it was still suspending
                    }
                    _log("window hidden: WebView2 memory target low" + (suspended ? ", page suspended" : ", page not suspended"));
                }
                else
                {
                    if (web.IsSuspended)
                    {
                        web.Resume();
                    }
                    web.MemoryUsageTargetLevel = CoreWebView2MemoryUsageTargetLevel.Normal;
                }
            }
            catch (Exception ex)
            {
                // Closed in the meantime, or an older WebView2 runtime without these calls: the page just stays as it was.
                _log("WebView2 memory setting failed: " + ex.Message);
            }
        }

        private void CloseWebView()
        {
            if (_controller == null)
            {
                return;
            }
            try
            {
                _controller.Close();
            }
            catch (Exception ex)
            {
                _log("closing the WebView2 failed: " + ex.Message);
            }
            _controller = null;
            _web = null;
        }

        protected override void Dispose(bool disposing)
        {
            if (disposing)
            {
                CloseWebView();
                _showTimer.Dispose();
                if (_bigIcon != null)
                {
                    _bigIcon.Dispose();
                }
                if (_smallIcon != null)
                {
                    _smallIcon.Dispose();
                }
            }
            base.Dispose(disposing);
        }

        // ---------- icon, title bar, DPI ----------

        private Icon LoadBigIcon()
        {
            try
            {
                if (_iconFile != null)
                {
                    _bigIcon = new Icon(_iconFile, 256, 256); // the taskbar button wants the large one
                    return _bigIcon;
                }
            }
            catch (Exception)
            {
            }
            try
            {
                _bigIcon = Icon.ExtractAssociatedIcon(Application.ExecutablePath);
                return _bigIcon;
            }
            catch (Exception)
            {
                return null;
            }
        }

        protected override void OnHandleCreated(EventArgs e)
        {
            base.OnHandleCreated(e);

            // Dark title bar, to match the dark UI.
            try
            {
                int on = 1;
                if (DwmSetWindowAttribute(Handle, DWMWA_USE_IMMERSIVE_DARK_MODE, ref on, 4) != 0)
                {
                    DwmSetWindowAttribute(Handle, DWMWA_USE_IMMERSIVE_DARK_MODE_OLD, ref on, 4);
                }
            }
            catch (Exception)
            {
                // Cosmetic only.
            }

            ApplySmallIcon();
        }

        // The title-bar icon at exactly the size this screen's DPI asks for, so it is not scaled (and blurry).
        private void ApplySmallIcon()
        {
            try
            {
                if (_iconFile == null)
                {
                    return;
                }
                int size = GetSystemMetricsForDpi(SM_CXSMICON, GetDpiForWindow(Handle));
                Icon small = new Icon(_iconFile, size, size);
                SendMessage(Handle, WM_SETICON, (IntPtr)ICON_SMALL, small.Handle);
                if (_smallIcon != null)
                {
                    _smallIcon.Dispose();
                }
                _smallIcon = small;
            }
            catch (Exception)
            {
                // Cosmetic only.
            }
        }

        // Moved to a screen with a different scale: take the size Windows suggests, so the window keeps its look.
        protected override void WndProc(ref Message m)
        {
            if (m.Msg == WM_DPICHANGED && !_placing)
            {
                RECT r = (RECT)Marshal.PtrToStructure(m.LParam, typeof(RECT));
                SetBounds(r.Left, r.Top, r.Right - r.Left, r.Bottom - r.Top);
                MinimumSize = ScaledSize(640, 420);
                ApplySmallIcon();
                m.Result = IntPtr.Zero;
                return;
            }
            base.WndProc(ref m);
        }

        private Size ScaledSize(int width, int height)
        {
            float scale = DeviceDpi / 96f;
            return new Size((int)(width * scale), (int)(height * scale));
        }

        // ---------- remembered position and size ----------

        // Use the saved bounds if they are still on a screen, else centre a default-sized window.
        private void PlaceWindow()
        {
            MinimumSize = ScaledSize(640, 420);

            Rectangle bounds;
            bool maximized;
            if (LoadState(out bounds, out maximized) && IsOnScreen(bounds))
            {
                _startMaximized = maximized;
            }
            else
            {
                Rectangle area = Screen.PrimaryScreen.WorkingArea;
                Size size = ScaledSize(DefaultWidth, DefaultHeight);
                size.Width = Math.Min(size.Width, area.Width);
                size.Height = Math.Min(size.Height, area.Height);
                bounds = new Rectangle(
                    area.Left + (area.Width - size.Width) / 2,
                    area.Top + (area.Height - size.Height) / 2,
                    size.Width,
                    size.Height);
            }

            _placing = true;
            try
            {
                SetBounds(bounds.X, bounds.Y, bounds.Width, bounds.Height);
            }
            finally
            {
                _placing = false;
            }
        }

        // The saved title-bar area must overlap a screen enough to grab, otherwise the saved spot is ignored
        // (a monitor that is no longer connected, say).
        private static bool IsOnScreen(Rectangle bounds)
        {
            if (bounds.Width < 300 || bounds.Height < 200 || bounds.Width > 20000 || bounds.Height > 20000)
            {
                return false;
            }
            Rectangle titleBar = new Rectangle(bounds.X, bounds.Y, bounds.Width, 40);
            foreach (Screen screen in Screen.AllScreens)
            {
                Rectangle overlap = Rectangle.Intersect(titleBar, screen.WorkingArea);
                if (overlap.Width >= 100 && overlap.Height >= 20)
                {
                    return true;
                }
            }
            return false;
        }

        // data\window-state.txt: plain "name=value" lines (left, top, width, height, maximized).
        private bool LoadState(out Rectangle bounds, out bool maximized)
        {
            bounds = Rectangle.Empty;
            maximized = false;
            try
            {
                if (!File.Exists(_stateFile))
                {
                    return false;
                }
                int left = 0, top = 0, width = 0, height = 0, max = 0;
                int found = 0;
                foreach (string line in File.ReadAllLines(_stateFile))
                {
                    int equals = line.IndexOf('=');
                    if (equals <= 0)
                    {
                        continue;
                    }
                    string name = line.Substring(0, equals).Trim().ToLowerInvariant();
                    int value;
                    if (!int.TryParse(line.Substring(equals + 1).Trim(), NumberStyles.Integer, CultureInfo.InvariantCulture, out value))
                    {
                        continue;
                    }
                    switch (name)
                    {
                        case "left": left = value; found++; break;
                        case "top": top = value; found++; break;
                        case "width": width = value; found++; break;
                        case "height": height = value; found++; break;
                        case "maximized": max = value; break;
                    }
                }
                if (found < 4)
                {
                    return false;
                }
                bounds = new Rectangle(left, top, width, height);
                maximized = max != 0;
                return true;
            }
            catch (Exception ex)
            {
                _log("could not read the saved window position: " + ex.Message);
                return false;
            }
        }

        private void SaveState()
        {
            if (!_shownOnce)
            {
                return; // never shown: keep whatever was saved
            }
            try
            {
                Rectangle b = WindowState == FormWindowState.Normal ? Bounds : RestoreBounds;
                Directory.CreateDirectory(Path.GetDirectoryName(_stateFile));
                File.WriteAllLines(_stateFile, new string[]
                {
                    "left=" + b.Left.ToString(CultureInfo.InvariantCulture),
                    "top=" + b.Top.ToString(CultureInfo.InvariantCulture),
                    "width=" + b.Width.ToString(CultureInfo.InvariantCulture),
                    "height=" + b.Height.ToString(CultureInfo.InvariantCulture),
                    "maximized=" + (_wasMaximized ? "1" : "0")
                });
            }
            catch (Exception ex)
            {
                _log("could not save the window position: " + ex.Message);
            }
        }
    }
}
