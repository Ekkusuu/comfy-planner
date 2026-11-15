// Comfy Planner launcher
//
// A small WinForms tray app that starts the local Comfy Planner server (FastAPI on
// 127.0.0.1:8765) if it is not already running, shows the UI in its own window
// (AppWindow.cs: a WinForms form hosting a WebView2), and keeps running in the
// notification area so the server keeps working after the window is closed.
//
// Built with the C# compiler that ships with Windows (.NET Framework), so the code
// is plain C# 5: no string interpolation, no null-conditional operators, etc.
// Build with launcher\build.ps1 (it also fetches the WebView2 SDK on first use).
//
// Command line:
//   "Comfy Planner.exe"          start (or focus) the app, starting the server if needed
//   "Comfy Planner.exe" --quit   ask a running instance to quit (used for testing)

using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Net;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Windows.Forms;

namespace ComfyPlanner
{
    internal static class Program
    {
        // Where the FastAPI app listens. Keep in sync with config/app.toml.
        public const string AppUrl = "http://127.0.0.1:8765";

        // Per-user-session names: one launcher, one "reopen the window" signal, one "quit" signal.
        internal const string MutexName = "Local\\ComfyPlanner.Mutex";
        internal const string OpenEventName = "Local\\ComfyPlanner.Open";
        internal const string QuitEventName = "Local\\ComfyPlanner.Quit";

        [DllImport("user32.dll")]
        private static extern bool AllowSetForegroundWindow(int processId);

        [STAThread]
        private static void Main(string[] args)
        {
            bool quit = false;
            for (int i = 0; i < args.Length; i++)
            {
                if (string.Equals(args[i], "--quit", StringComparison.OrdinalIgnoreCase))
                {
                    quit = true;
                }
            }

            // Hidden switch used by build/test scripts: signal the running instance to quit.
            if (quit)
            {
                Signal(QuitEventName);
                return;
            }

            // Single instance. The second copy asks the first to show its window, then leaves.
            bool created;
            Mutex mutex = new Mutex(true, MutexName, out created);
            if (!created)
            {
                // This process was just started by the user, so it may hand the foreground right to the first one.
                AllowSetForegroundWindow(-1);
                Signal(OpenEventName);
                return;
            }

            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);

            // The project folder is always the folder that contains the exe.
            string root = Path.GetDirectoryName(Application.ExecutablePath);

            using (TrayContext context = new TrayContext(root))
            {
                Application.Run(context);
            }

            // The mutex is released when the process exits; this keeps the reference alive.
            GC.KeepAlive(mutex);
        }

        // Set a named event. Retries briefly in case the first instance is still starting up.
        private static void Signal(string name)
        {
            for (int attempt = 0; attempt < 25; attempt++)
            {
                try
                {
                    using (EventWaitHandle handle = EventWaitHandle.OpenExisting(name))
                    {
                        handle.Set();
                        return;
                    }
                }
                catch (WaitHandleCannotBeOpenedException)
                {
                    Thread.Sleep(100); // no launcher yet; give it a moment
                }
                catch (Exception)
                {
                    return;
                }
            }
        }
    }

    // Owns the tray icon, the startup thread and the server process.
    internal sealed class TrayContext : ApplicationContext
    {
        private readonly string _root;
        private readonly string _logsDir;
        private readonly string _logPath;      // data\logs\app.log (server output)
        private readonly NotifyIcon _tray;
        private readonly ContextMenuStrip _menu;
        private readonly System.Windows.Forms.Timer _timer;
        private readonly EventWaitHandle _openEvent;
        private readonly EventWaitHandle _quitEvent;
        private readonly RegisteredWaitHandle _openWait;
        private readonly RegisteredWaitHandle _quitWait;
        private readonly Thread _startup;

        private readonly object _logLock = new object();
        private readonly object _serverLock = new object();

        private StreamWriter _backendLog;
        private Process _backend;              // non-null only if THIS launcher started the server
        private AppWindow _window;             // the app window; hidden (not closed) when the user clicks X
        private bool _windowUnavailable;       // WebView2 could not start: use the default browser instead
        private volatile bool _openRequested;
        private volatile bool _quitRequested;
        private volatile bool _starting = true;
        private volatile string _message;      // queued MessageBox text, shown on the UI thread
        private volatile string[] _balloon;    // queued { title, text } balloon
        private bool _quitting;
        private bool _firstStart;              // no .venv yet: the first "uv sync" sets the environment up (one time)

        public TrayContext(string root)
        {
            _root = root;
            _logsDir = Path.Combine(_root, "data\\logs");
            _logPath = Path.Combine(_logsDir, "app.log");

            try
            {
                Directory.CreateDirectory(_logsDir);
            }
            catch (Exception)
            {
                // Logging is best effort; the app can still run.
            }

            Log("launcher started from " + _root);

            // Named events: the thread pool just flags them, the UI timer does the work.
            _openEvent = new EventWaitHandle(false, EventResetMode.AutoReset, Program.OpenEventName);
            _quitEvent = new EventWaitHandle(false, EventResetMode.AutoReset, Program.QuitEventName);
            _openWait = ThreadPool.RegisterWaitForSingleObject(_openEvent, OnOpenSignal, null, Timeout.Infinite, false);
            _quitWait = ThreadPool.RegisterWaitForSingleObject(_quitEvent, OnQuitSignal, null, Timeout.Infinite, false);

            // Tray menu.
            _menu = new ContextMenuStrip();
            ToolStripMenuItem versionItem = new ToolStripMenuItem("Comfy Planner " + AppVersion());
            versionItem.Enabled = false;      // a header that shows the version, not a command
            _menu.Items.Add(versionItem);
            _menu.Items.Add(new ToolStripSeparator());
            _menu.Items.Add("Open", null, new EventHandler(OnOpenClick));
            _menu.Items.Add("Open logs folder", null, new EventHandler(OnLogsClick));
            _menu.Items.Add(new ToolStripSeparator());
            _menu.Items.Add("Quit", null, new EventHandler(OnQuitClick));

            _tray = new NotifyIcon();
            _tray.Icon = LoadAppIcon();
            _tray.Text = "Comfy Planner " + AppVersion();
            _tray.ContextMenuStrip = _menu;
            _tray.DoubleClick += new EventHandler(OnOpenClick);
            _tray.Visible = true;

            // WinForms timer: every event from other threads funnels through this on the UI thread.
            _timer = new System.Windows.Forms.Timer();
            _timer.Interval = 250;
            _timer.Tick += new EventHandler(OnTick);
            _timer.Start();

            // Startup (UI build, server, health) runs off the UI thread so the tray is responsive.
            _startup = new Thread(RunStartup);
            _startup.IsBackground = true;
            _startup.Name = "Comfy Planner startup";
            _startup.Start();
        }

        // ---------- tray / window ----------

        // A click on the tray icon or its Open item shows the window straight away (on the click itself, so
        // Windows lets it take the foreground).
        private void OnOpenClick(object sender, EventArgs e)
        {
            OpenFromUser();
        }

        private void OpenFromUser()
        {
            if (_starting)
            {
                Balloon("Comfy Planner", "Still starting, one moment...");
            }
            else
            {
                OpenWindow();
            }
        }

        private void OnQuitClick(object sender, EventArgs e)
        {
            _quitRequested = true;
        }

        private void OnOpenSignal(object state, bool timedOut)
        {
            if (!timedOut)
            {
                _openRequested = true;
            }
        }

        private void OnQuitSignal(object state, bool timedOut)
        {
            if (!timedOut)
            {
                _quitRequested = true;
            }
        }

        private void OnTick(object sender, EventArgs e)
        {
            if (_quitRequested)
            {
                Quit();
                return;
            }

            string message = _message;
            if (message != null)
            {
                _message = null;
                MessageBox.Show(message, "Comfy Planner", MessageBoxButtons.OK, MessageBoxIcon.Error);
            }

            string[] balloon = _balloon;
            if (balloon != null)
            {
                _balloon = null;
                try
                {
                    _tray.ShowBalloonTip(4000, balloon[0], balloon[1], ToolTipIcon.Info);
                }
                catch (Exception)
                {
                    // Balloons are cosmetic.
                }
            }

            if (_openRequested)
            {
                _openRequested = false;
                OpenFromUser();
            }
        }

        // Show the app window: create it on first use, bring it back if it was hidden or minimized.
        private void OpenWindow()
        {
            if (_windowUnavailable)
            {
                OpenInBrowser();
                return;
            }

            if (_window == null || _window.IsDisposed)
            {
                _window = new AppWindow(_root, Program.AppUrl, Log);
                Log("created the app window");
                StartWindow(_window);
            }
            else
            {
                Log("showing the app window");
            }
            _window.Present();
        }

        // Start the WebView2 in the new window. If it cannot start (the WebView2 runtime is missing), tell the
        // user once and use the default browser from then on.
        private async void StartWindow(AppWindow window)
        {
            string problem = await window.StartAsync();
            if (problem == null)
            {
                return;
            }

            Log("the app window is not available (" + problem + "); using the default browser");
            _windowUnavailable = true;
            _window = null;
            window.Shutdown();
            OpenInBrowser();
            MessageBox.Show(
                "Comfy Planner could not start its own window. It needs the Microsoft Edge WebView2 Runtime, "
                + "which was not found on this PC.\r\n\r\n"
                + "Comfy Planner opens in your default browser instead. To get the app window, install the runtime "
                + "(search for \"WebView2 Runtime\" on microsoft.com) and start Comfy Planner again.",
                "Comfy Planner", MessageBoxButtons.OK, MessageBoxIcon.Information);
        }

        // Fallback: the UI in the default browser.
        private void OpenInBrowser()
        {
            try
            {
                Process.Start(Program.AppUrl);
                Log("opened the default browser");
            }
            catch (Exception ex)
            {
                Log("could not open a browser: " + ex.Message);
                _message = "Could not open the UI. Visit " + Program.AppUrl + " in your browser.";
            }
        }

        private void OnLogsClick(object sender, EventArgs e)
        {
            try
            {
                Directory.CreateDirectory(_logsDir);
                Process.Start("explorer.exe", "\"" + _logsDir + "\"");
            }
            catch (Exception ex)
            {
                Log("could not open the logs folder: " + ex.Message);
            }
        }

        // ---------- startup ----------

        private void RunStartup()
        {
            bool open = false;
            try
            {
                if (!EnsureUi())
                {
                    return; // the failure message is already queued
                }
                if (_quitRequested)
                {
                    return;
                }

                EnsureBackend();
                if (_quitRequested)
                {
                    return;
                }
                if (_message != null)
                {
                    return; // the server could not be started; the message is already queued
                }

                // Normally a few seconds; a cold start (antivirus scanning the venv after a reboot) can take
                // much longer, so wait long. A server that crashes is noticed right away instead.
                // The very first start also downloads and installs the Python packages, so it gets longer.
                int waitSeconds = _firstStart ? 900 : 300;
                string health = WaitForHealth(waitSeconds);
                if (health == null)
                {
                    if (_quitRequested)
                    {
                        return;
                    }
                    bool exited = BackendExited();
                    Log(exited ? "the server exited before it answered" : "the app did not answer on " + Program.AppUrl + " within " + (waitSeconds / 60) + " minutes");
                    _message = (exited ? "The Comfy Planner server stopped while starting."
                                       : "Comfy Planner did not answer on " + Program.AppUrl + " within " + (waitSeconds / 60) + " minutes.")
                        + "\r\n\r\nCheck the log:\r\n" + _logPath;
                    return;
                }

                // Best effort: if ComfyUI is down, ask the app to start it. The UI shows the real state.
                TryStartComfy(health);
                open = true;
            }
            catch (Exception ex)
            {
                Log("startup failed: " + ex);
                _message = "Comfy Planner could not start:\r\n" + ex.Message + "\r\n\r\nSee " + _logPath;
            }
            finally
            {
                _starting = false;
                if (open)
                {
                    _openRequested = true;
                }
            }
        }

        // First run: build the UI if ui\dist is missing.
        private bool EnsureUi()
        {
            string index = Path.Combine(_root, "ui\\dist\\index.html");
            if (File.Exists(index))
            {
                return true;
            }

            Log("ui/dist/index.html is missing; building the UI");
            Balloon("Comfy Planner", "Setting up: building the UI for the first time...");

            // Build output goes to the same log, so failures are easy to inspect.
            RunHidden(Path.Combine(_root, "ui"), "npm ci");
            RunHidden(Path.Combine(_root, "ui"), "npm run build");

            if (!File.Exists(index))
            {
                _message = "The UI could not be built (npm ci and npm run build in ui\\).\r\n\r\n"
                    + "See the log:\r\n" + _logPath;
                return false;
            }
            return true;
        }

        // Run a command in a hidden window, appending its output to app.log.
        private bool RunHidden(string workDir, string command)
        {
            try
            {
                ProcessStartInfo info = new ProcessStartInfo(
                    "cmd.exe", "/c " + command + " >> \"" + _logPath + "\" 2>&1");
                info.WorkingDirectory = workDir;
                info.UseShellExecute = false;
                info.CreateNoWindow = true;

                using (Process process = Process.Start(info))
                {
                    if (process == null)
                    {
                        return false;
                    }
                    process.WaitForExit();
                    Log("setup: \"" + command + "\" exited with " + process.ExitCode);
                    return process.ExitCode == 0;
                }
            }
            catch (Exception ex)
            {
                Log("setup: \"" + command + "\" failed: " + ex.Message);
                return false;
            }
        }

        // Start the FastAPI server if it is not already listening. Remember if we started it.
        private void EnsureBackend()
        {
            if (IsAppUp(Get(Program.AppUrl + "/api/health")))
            {
                Log("a server is already running on 8765; reusing it and leaving it alone");
                return;
            }

            string uv = FindUv();
            if (uv == null)
            {
                Log("uv.exe was not found");
                _message = "uv.exe was not found next to Comfy Planner, on PATH or in %USERPROFILE%\\.local\\bin.\r\n\r\n"
                    + "Install uv (https://docs.astral.sh/uv/) and try again.";
                return;
            }

            // The installed copy carries its own uv.exe. Then the packages come from uv.lock exactly (--frozen)
            // and without the test tools (--no-dev). A checkout that uses uv from PATH keeps the plain "uv sync".
            bool bundled = string.Equals(uv, Path.Combine(_root, "uv.exe"), StringComparison.OrdinalIgnoreCase);
            // From a checkout, --inexact keeps packages a developer added to .venv by hand (as "uv run" did).
            string syncArgs = bundled ? "sync --frozen --no-dev" : "sync --inexact";

            // uv only sets the environment up. The server is then started with the environment's own python, so
            // no uv process stays alive (and holds memory) for as long as the server runs.
            string python = Path.Combine(_root, ".venv\\Scripts\\python.exe");
            string serverArgs = "-m uvicorn app.main:app --host 127.0.0.1 --port 8765";

            // First start: the .venv is created and the packages are installed by the first "uv sync" below.
            _firstStart = !Directory.Exists(Path.Combine(_root, ".venv"));
            if (_firstStart)
            {
                Log("no .venv yet; the first start sets up the Python environment (one time)");
                Balloon("Comfy Planner", "First start: setting up (one time). This can take a few minutes.");
            }

            try
            {
                Directory.CreateDirectory(_logsDir);
                _backendLog = new StreamWriter(
                    new FileStream(_logPath, FileMode.Append, FileAccess.Write, FileShare.ReadWrite),
                    new UTF8Encoding(false));
                _backendLog.AutoFlush = true;

                // Step 1: bring the Python environment up to date, and wait for it. On the very first start this
                // downloads and installs the packages, so it gets the longer limit.
                int syncSeconds = _firstStart ? 900 : 300;
                Process sync = StartLogged(uv, syncArgs);
                if (sync == null)
                {
                    return; // quit was clicked
                }
                Log("syncing the Python environment: uv " + syncArgs + " (pid " + sync.Id + ")");
                if (!sync.WaitForExit(syncSeconds * 1000))
                {
                    StopBackend(); // kills the stuck uv; it is the process held in _backend
                    Log("uv sync did not finish within " + (syncSeconds / 60) + " minutes");
                    _message = "Setting up the Python environment did not finish within " + (syncSeconds / 60) + " minutes.\r\n\r\n"
                        + "Check the log:\r\n" + _logPath;
                    return;
                }
                sync.WaitForExit(); // lets the output handlers write the last lines
                int syncCode = sync.ExitCode;
                lock (_serverLock)
                {
                    if (_backend == sync)
                    {
                        _backend = null;
                    }
                }
                sync.Dispose();
                if (_quitRequested)
                {
                    return;
                }
                if (syncCode != 0 || !File.Exists(python))
                {
                    Log("uv sync failed (exit code " + syncCode + ")");
                    _message = "The Python environment could not be set up (uv sync failed).\r\n\r\n"
                        + "Check the log:\r\n" + _logPath;
                    return;
                }

                // Step 2: the server itself, started directly with the environment's python.
                Process process = StartLogged(python, serverArgs);
                if (process == null)
                {
                    return; // quit was clicked
                }
                Log("started the server with " + python + " (pid " + process.Id + ")");
            }
            catch (Exception ex)
            {
                Log("the server could not be started: " + ex.Message);
                _message = "The server could not be started: " + ex.Message + "\r\n\r\nSee " + _logPath;
            }
        }

        // Start a hidden process in the project folder, with its output going to app.log, and remember it in
        // _backend so that Quit can stop it. Returns null (and starts nothing) when quit has been requested.
        private Process StartLogged(string file, string args)
        {
            if (_quitRequested)
            {
                return null;
            }

            ProcessStartInfo info = new ProcessStartInfo(file, args);
            info.WorkingDirectory = _root;
            info.UseShellExecute = false;
            info.CreateNoWindow = true;               // no console window
            info.RedirectStandardOutput = true;
            info.RedirectStandardError = true;
            info.StandardOutputEncoding = Encoding.UTF8;
            info.StandardErrorEncoding = Encoding.UTF8;
            info.EnvironmentVariables["PYTHONUNBUFFERED"] = "1";

            Process process = new Process();
            process.StartInfo = info;
            process.OutputDataReceived += OnBackendOutput;
            process.ErrorDataReceived += OnBackendOutput;
            process.Start();
            process.BeginOutputReadLine();
            process.BeginErrorReadLine();

            lock (_serverLock)
            {
                _backend = process;
            }
            if (_quitRequested)
            {
                StopBackend(); // quit was clicked while we were starting
            }
            return process;
        }

        private void OnBackendOutput(object sender, DataReceivedEventArgs e)
        {
            if (e.Data == null)
            {
                return;
            }
            lock (_logLock)
            {
                try
                {
                    if (_backendLog != null)
                    {
                        _backendLog.WriteLine(e.Data);
                    }
                }
                catch (Exception)
                {
                    // Never let logging take the app down.
                }
            }
        }

        // Poll /api/health until it answers. Returns the JSON body, or null on timeout.
        private string WaitForHealth(int seconds)
        {
            Stopwatch clock = Stopwatch.StartNew();
            bool slowNoticeShown = false;
            while (clock.Elapsed.TotalSeconds < seconds)
            {
                if (_quitRequested || BackendExited())
                {
                    return null;
                }
                string json = Get(Program.AppUrl + "/api/health");
                if (IsAppUp(json))
                {
                    return json;
                }
                if (!slowNoticeShown && clock.Elapsed.TotalSeconds > 20)
                {
                    slowNoticeShown = true;
                    Balloon("Comfy Planner", _firstStart
                        ? "First start: setting up (one time). Still working, this can take a few minutes..."
                        : "Still starting, this is taking longer than usual...");
                }
                Thread.Sleep(500);
            }
            return null;
        }

        // True if we started the server and its process has already exited.
        private bool BackendExited()
        {
            lock (_serverLock)
            {
                try
                {
                    return _backend != null && _backend.HasExited;
                }
                catch (Exception)
                {
                    return false;
                }
            }
        }

        // If ComfyUI is not reachable, ask the app to start it. Failures are ignored on purpose.
        private void TryStartComfy(string healthJson)
        {
            if (ComfyReachable(healthJson))
            {
                Log("ComfyUI is reachable");
                return;
            }

            Log("ComfyUI is not reachable; asking the app to start it");
            try
            {
                HttpWebRequest request = (HttpWebRequest)WebRequest.Create(Program.AppUrl + "/api/comfy/start");
                request.Method = "POST";
                request.ContentLength = 0;
                request.Timeout = 5000;
                request.ReadWriteTimeout = 5000;
                request.Proxy = null;
                request.KeepAlive = false;
                using (HttpWebResponse response = (HttpWebResponse)request.GetResponse())
                using (StreamReader reader = new StreamReader(response.GetResponseStream()))
                {
                    reader.ReadToEnd(); // drain
                }
            }
            catch (Exception ex)
            {
                // The UI shows the status, so a slow or failed start is not fatal here.
                Log("POST /api/comfy/start failed (ignored): " + ex.Message);
            }
        }

        // ---------- HTTP / JSON (tiny, no external libraries) ----------

        private static string Get(string url)
        {
            try
            {
                HttpWebRequest request = (HttpWebRequest)WebRequest.Create(url);
                request.Method = "GET";
                // /api/health also probes ComfyUI; when ComfyUI is down, Windows takes ~2 s to refuse that
                // connection, so a short timeout here would never see the app as up.
                request.Timeout = 10000;
                request.ReadWriteTimeout = 10000;
                request.Proxy = null;
                request.KeepAlive = false;
                using (HttpWebResponse response = (HttpWebResponse)request.GetResponse())
                using (StreamReader reader = new StreamReader(response.GetResponseStream(), Encoding.UTF8))
                {
                    if ((int)response.StatusCode != 200)
                    {
                        return null;
                    }
                    return reader.ReadToEnd();
                }
            }
            catch (Exception)
            {
                return null; // not listening yet, refused, timed out...
            }
        }

        private static string Compact(string json)
        {
            if (json == null)
            {
                return "";
            }
            return json.Replace(" ", "").Replace("\t", "").Replace("\r", "").Replace("\n", "");
        }

        // /api/health returns {"app":"ok","comfy":{...},...}
        private static bool IsAppUp(string json)
        {
            return Compact(json).IndexOf("\"app\":\"ok\"", StringComparison.Ordinal) >= 0;
        }

        private static bool ComfyReachable(string json)
        {
            string compact = Compact(json);
            int start = compact.IndexOf("\"comfy\":", StringComparison.Ordinal);
            return start >= 0 && compact.IndexOf("\"reachable\":true", start, StringComparison.Ordinal) >= 0;
        }

        // ---------- quit ----------

        private void Quit()
        {
            if (_quitting)
            {
                return;
            }
            _quitting = true;

            _timer.Stop();
            try
            {
                _openWait.Unregister(null);
            }
            catch (Exception)
            {
            }
            try
            {
                _quitWait.Unregister(null);
            }
            catch (Exception)
            {
            }

            try
            {
                _tray.Visible = false;
                _tray.Dispose();
                _menu.Dispose();
            }
            catch (Exception)
            {
            }

            if (_window != null)
            {
                try
                {
                    _window.Shutdown(); // closes the WebView2 so its browser processes end
                }
                catch (Exception ex)
                {
                    Log("closing the window failed: " + ex.Message);
                }
                _window = null;
            }

            StopBackend();

            try
            {
                _openEvent.Close();
                _quitEvent.Close();
            }
            catch (Exception)
            {
            }

            Log("launcher exiting");
            ExitThread();
        }

        // Kill only the server process tree that this launcher started. Never anything else.
        private void StopBackend()
        {
            Process backend = null;
            lock (_serverLock)
            {
                backend = _backend;
                _backend = null;
            }
            if (backend == null)
            {
                return; // a server we did not start stays up
            }

            try
            {
                if (!backend.HasExited)
                {
                    Log("stopping the server (pid " + backend.Id + ")");
                    ProcessStartInfo info = new ProcessStartInfo(
                        "taskkill", "/PID " + backend.Id + " /T /F");
                    info.UseShellExecute = false;
                    info.CreateNoWindow = true;
                    using (Process killer = Process.Start(info))
                    {
                        if (killer != null)
                        {
                            killer.WaitForExit(10000);
                        }
                    }
                    backend.WaitForExit(5000);
                }
            }
            catch (Exception ex)
            {
                Log("stopping the server failed: " + ex.Message);
            }
            finally
            {
                lock (_logLock)
                {
                    try
                    {
                        if (_backendLog != null)
                        {
                            _backendLog.Dispose();
                        }
                    }
                    catch (Exception)
                    {
                    }
                    _backendLog = null;
                }
            }
        }

        // ---------- helpers ----------

        private void Balloon(string title, string text)
        {
            _balloon = new string[] { title, text };
        }

        // The version stamped into the exe by launcher\build.ps1 (the product version of its file properties).
        private static string AppVersion()
        {
            try
            {
                string version = FileVersionInfo.GetVersionInfo(Application.ExecutablePath).ProductVersion;
                return string.IsNullOrEmpty(version) ? "" : version;
            }
            catch (Exception)
            {
                return "";
            }
        }

        private Icon LoadAppIcon()
        {
            try
            {
                string file = Path.Combine(_root, "launcher\\icon.ico");
                if (File.Exists(file))
                {
                    return new Icon(file, SystemInformation.SmallIconSize);
                }
            }
            catch (Exception)
            {
            }
            try
            {
                return Icon.ExtractAssociatedIcon(Application.ExecutablePath);
            }
            catch (Exception)
            {
            }
            return SystemIcons.Application;
        }

        // uv.exe next to this exe (the installed copy), then PATH, then the standard per-user install location.
        private string FindUv()
        {
            try
            {
                string bundled = Path.Combine(_root, "uv.exe");
                if (File.Exists(bundled))
                {
                    return bundled;
                }
            }
            catch (Exception)
            {
            }

            try
            {
                string path = Environment.GetEnvironmentVariable("PATH");
                if (path != null)
                {
                    string[] parts = path.Split(';');
                    for (int i = 0; i < parts.Length; i++)
                    {
                        string dir = parts[i].Trim();
                        if (dir.Length == 0)
                        {
                            continue;
                        }
                        string exe = Path.Combine(dir, "uv.exe");
                        if (File.Exists(exe))
                        {
                            return exe;
                        }
                    }
                }
            }
            catch (Exception)
            {
            }

            try
            {
                string fallback = Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
                    ".local\\bin\\uv.exe");
                if (File.Exists(fallback))
                {
                    return fallback;
                }
            }
            catch (Exception)
            {
            }
            return null;
        }

        private void Log(string line)
        {
            try
            {
                string path = Path.Combine(_logsDir, "launcher.log");
                File.AppendAllText(
                    path,
                    DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + "  " + line + Environment.NewLine,
                    new UTF8Encoding(false));
            }
            catch (Exception)
            {
                // Logging must never break the launcher.
            }
        }
    }
}
