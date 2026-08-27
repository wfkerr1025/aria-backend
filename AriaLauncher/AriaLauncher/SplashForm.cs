using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;
using System;
using System.Diagnostics;
using System.IO;
using System.Threading.Tasks;
using System.Windows.Forms;

namespace AriaLauncher
{
	public partial class SplashForm : Form
	{
		private WebView2 _webView;
		private CoreWebView2? _core;
		private Process? _backendProc;
		private Process? _restApiProc;
		private Process? _electronProc;

		public SplashForm()
		{
			UnifiedLogger.Log("Launcher", "SplashForm constructor entered.");

			AutoScaleMode = AutoScaleMode.None;

			FormBorderStyle = FormBorderStyle.None;
			StartPosition = FormStartPosition.CenterScreen;

			// 960x540 is a 96-DPI (100%) design size. The app is PerMonitorV2 DPI-aware,
			// so Windows hands us real physical pixels and expects us to scale ourselves —
			// without this, the splash renders at literal 960x540 px and looks tiny on any
			// display above 100% scaling.
			float dpiScale = DeviceDpi / 96f;
			var designSize = new System.Drawing.Size(
				(int)Math.Round(960 * dpiScale),
				(int)Math.Round(540 * dpiScale));

			ClientSize = designSize;
			MinimumSize = designSize;

			TopMost = true;
			ShowInTaskbar = false;
			DoubleBuffered = true;

			UnifiedLogger.Log("Launcher", "Creating WebView2 control...");

			_webView = new WebView2
			{
				Dock = DockStyle.Fill
			};

			Controls.Add(_webView);
			PerformLayout();

			UnifiedLogger.Log("Launcher", "WebView2 control added to form.");

			Shown += SplashForm_Shown;
			FormClosing += SplashForm_FormClosing;
			Resize += SplashForm_Resize;
		}

		private void SplashForm_Resize(object? sender, EventArgs e)
		{
			try
			{
				// Keep WebView2 aligned with the form’s client area
				_webView.Bounds = this.ClientRectangle;
			}
			catch (Exception ex)
			{
				UnifiedLogger.LogError("WebView2", "Exception during SplashForm_Resize", ex);
				UnifiedRemoteLogger.LogException("WebView2", ex);
			}
		}

		private async void SplashForm_Shown(object? sender, EventArgs e)
		{
			UnifiedLogger.Log("Launcher", "SplashForm shown — beginning startup sequence.");

			await InitializeWebView();
			await RunInitializationPipeline();
			await RunLaunchSequence();
		}

		// Heavy model-catalog work (scanning ~/.aria-lite/models, GGUF
		// metadata extraction, requirement derivation, and caching the
		// result) runs here, before the backend/Electron even start, so
		// the very first real use of that data (Models page, safety
		// checks) is a cache hit — see backend/core/init_pipeline.py and
		// BackendManager.RunModelScanAndCache() and friends. Each step's
		// own failure is logged and swallowed rather than aborting the
		// launch: a missed cache warm-up just means the app computes
		// that data on first use instead of at startup, the same
		// fallback backend/core/compatibility_checker.py and
		// model_loader.py already have for a cold cache.
		private async Task RunInitializationPipeline()
		{
			PostStatus("[Launcher] Scanning models…");
			if (!await BackendManager.RunModelScanAndCache())
				UnifiedLogger.Log("InitPipeline WARN", "Model scan step failed — continuing (non-fatal).");

			PostStatus("[Launcher] Parsing GGUF headers…");
			if (!await BackendManager.RunGGUFMetadataPass())
				UnifiedLogger.Log("InitPipeline WARN", "GGUF metadata pass failed — continuing (non-fatal).");

			PostStatus("[Launcher] Estimating requirements…");
			if (!await BackendManager.RunRequirementDerivation())
				UnifiedLogger.Log("InitPipeline WARN", "Requirement derivation failed — continuing (non-fatal).");

			PostStatus("[Launcher] Caching results…");
			if (!await BackendManager.WriteRequirementCache())
				UnifiedLogger.Log("InitPipeline WARN", "Requirement cache write failed — continuing (non-fatal).");

			PostStatus("[Launcher] Initialization complete.");
		}

		private void SplashForm_FormClosing(object? sender, FormClosingEventArgs e)
		{
			UnifiedLogger.Log("Launcher", "SplashForm closing.");

			if (_electronProc != null)
			{
				UnifiedLogger.Log("Electron", "Electron exit code: " +
					(_electronProc.HasExited ? _electronProc.ExitCode : -1));
			}

			if (_restApiProc != null)
			{
				UnifiedLogger.Log("RestApi", "Shutting down REST API from SplashForm_FormClosing.");
				RestApiManager.ShutdownRestApi(_restApiProc);
			}

			if (_backendProc != null)
			{
				UnifiedLogger.Log("Backend", "Shutting down backend from SplashForm_FormClosing.");
				BackendManager.ShutdownBackend(_backendProc);
			}

			UnifiedLogger.Log("Launcher", "=== SPLASHFORM END ===");
		}

		private async Task InitializeWebView()
		{
			UnifiedLogger.Log("WebView2", "InitializeWebView() entered.");

			try
			{
				string ariaRoot = @"D:\Users\William\ARIA-Lite Development\ARIA-Lite";

				string splashFolder = Path.Combine(
					ariaRoot, "AriaLauncher", "AriaLauncher", "splash");

				string webviewDataFolder = Path.Combine(
					ariaRoot, "AriaLauncher", "AriaLauncher", "WebView2Data");

				Directory.CreateDirectory(splashFolder);
				Directory.CreateDirectory(webviewDataFolder);

				UnifiedLogger.Log("WebView2", "splashFolder = " + splashFolder);
				UnifiedLogger.Log("WebView2", "webviewDataFolder = " + webviewDataFolder);

				UnifiedLogger.Log("WebView2", "Creating WebView2 environment...");
				var env = await CoreWebView2Environment.CreateAsync(null, webviewDataFolder);
				UnifiedLogger.Log("WebView2", "Environment created.");

				UnifiedLogger.Log("WebView2", "Calling EnsureCoreWebView2Async...");
				await _webView.EnsureCoreWebView2Async(env);
				UnifiedLogger.Log("WebView2", "EnsureCoreWebView2Async completed.");

				_core = _webView.CoreWebView2;
				UnifiedLogger.Log("WebView2", "CoreWebView2 = " + (_core != null));

				if (_core == null)
				{
					UnifiedLogger.Log("WebView2 ERROR", "CoreWebView2 is NULL — splash cannot load.");
					return;
				}

				// ⭐ Ensure WebView2 respects the form’s size and DPI
				_webView.ZoomFactor = 1.0;
				_webView.Bounds = this.ClientRectangle;

				UnifiedLogger.Log("WebView2", "Applying virtual host mapping...");
				_core.SetVirtualHostNameToFolderMapping(
					"local.splash",
					splashFolder,
					CoreWebView2HostResourceAccessKind.Allow);
				UnifiedLogger.Log("WebView2", "Virtual host mapping applied.");

				UnifiedLogger.Log("WebView2", "Navigating to splash.html...");
				_core.Navigate("https://local.splash/splash.html");
				UnifiedLogger.Log("WebView2", "Navigation requested.");
			}
			catch (Exception ex)
			{
				UnifiedLogger.LogError("WebView2", "Exception during InitializeWebView()", ex);
				UnifiedRemoteLogger.LogException("WebView2", ex);
				MessageBox.Show("Failed to initialize splash UI.\n\n" + ex.Message,
					"Launcher Error", MessageBoxButtons.OK, MessageBoxIcon.Error);
			}
		}

		private async Task RunLaunchSequence()
		{
			UnifiedLogger.Log("Launcher", "Starting launch sequence.");
			UnifiedRemoteLogger.Log("Launcher", "INFO", "=== ARIA LAUNCHER STARTUP ===");

			// Logging server starts FIRST — every other process (backend,
			// Electron, webui) logs into the one file it owns for this run.
			var loggingServerProc = BackendManager.StartLoggingServer();
			if (loggingServerProc == null)
			{
				// Non-fatal: UnifiedRemoteLogger silently no-ops if the log
				// server never came up, so the rest of the app still runs.
				UnifiedLogger.Log("LogServer WARN", "Failed to start logging server — continuing without unified remote logging.");
			}
			else
			{
				bool logServerReady = await BackendManager.WaitForLoggingServerReady(loggingServerProc);
				if (!logServerReady)
				{
					UnifiedLogger.Log("LogServer WARN", "Logging server did not signal readiness in time — continuing without unified remote logging.");
				}
			}

			_backendProc = BackendManager.StartBackend();
			if (_backendProc == null)
			{
				ShowError("Failed to start backend.");
				UnifiedRemoteLogger.Log("Launcher", "ERROR", "Failed to start backend.");
				await Task.Delay(3000);
				Close();
				return;
			}

			bool ready = await WaitForBackendReady(_backendProc);
			if (!ready)
			{
				ShowError("Backend did not signal readiness in time.");
				UnifiedRemoteLogger.Log("Launcher", "ERROR", "Backend did not signal readiness in time.");
				BackendManager.ShutdownBackend(_backendProc);
				await Task.Delay(3000);
				Close();
				return;
			}

			UnifiedLogger.Log("Backend", "Backend signaled readiness.");
			PostStatus("[Launcher] Backend ready. Starting frontend...");

			// REST API (/v1/*) — new, optional surface; the WebUI/Electron
			// path below never depends on it, so any failure here is
			// logged and swallowed rather than aborting the launch (see
			// RestApiManager.cs's module comment for the full rationale).
			_restApiProc = RestApiManager.StartRestApi(BackendManager.SelectedPort);
			if (_restApiProc != null)
			{
				bool restReady = await RestApiManager.WaitForRestApiReady(_restApiProc);
				UnifiedLogger.Log("RestApi", restReady
					? "REST API signaled readiness."
					: "REST API not confirmed ready — continuing launch regardless (non-fatal).");
			}
			else
			{
				UnifiedLogger.Log("RestApi WARN", "REST API failed to start — continuing launch without it (non-fatal).");
			}

			await Task.Delay(800);

			_electronProc = ElectronManager.StartElectron();
			if (_electronProc == null)
			{
				ShowError("Failed to start Electron frontend.");
				UnifiedRemoteLogger.Log("Launcher", "ERROR", "Failed to start Electron frontend.");
				RestApiManager.ShutdownRestApi(_restApiProc);
				BackendManager.ShutdownBackend(_backendProc);
				await Task.Delay(3000);
				Close();
				return;
			}

			UnifiedLogger.Log("Launcher", "Closing splash; handing control to Electron.");
			Hide();

			await Task.Run(() => _electronProc.WaitForExit());

			int electronExitCode = _electronProc.ExitCode;
			UnifiedLogger.Log("Electron", "Electron exited — shutting down backend.");
			UnifiedRemoteLogger.Log("Electron", electronExitCode == 0 ? "INFO" : "ERROR",
				"Electron exited, code = " + electronExitCode);

			RestApiManager.ShutdownRestApi(_restApiProc);
			BackendManager.ShutdownBackend(_backendProc);
			BackendManager.ShutdownLoggingServer();
			Close();
		}

		private async Task<bool> WaitForBackendReady(Process backend)
		{
			const string READY_TOKEN = "WS_SERVER_READY";
			bool ready = false;

			UnifiedLogger.Log("Backend", "Waiting for backend READY token...");

			backend.OutputDataReceived += (s, e) =>
			{
				if (!string.IsNullOrEmpty(e.Data) && e.Data.Contains(READY_TOKEN))
				{
					UnifiedLogger.Log("Backend", "Detected backend READY token.");
					ready = true;
				}
			};

			int timeoutMs = 15000;
			int elapsed = 0;

			UnifiedLogger.Log("Backend", $"Backend READY timeout = {timeoutMs} ms");

			while (!ready && elapsed < timeoutMs)
			{
				await Task.Delay(100);
				elapsed += 100;

				if (backend.HasExited)
				{
					UnifiedLogger.Log("Backend ERROR", "Backend exited before READY.");
					return false;
				}
			}

			if (!ready)
				UnifiedLogger.Log("Backend WARN", "Backend did not signal READY within timeout.");

			return ready;
		}

		private void PostStatus(string message)
		{
			UnifiedLogger.Log("Launcher", "PostStatus: " + message);

			try
			{
				_core?.PostWebMessageAsString(message);
			}
			catch (Exception ex)
			{
				UnifiedLogger.LogError("Launcher", "Failed to post status to splash", ex);
				UnifiedRemoteLogger.Log("Launcher", "ERROR", "IPC failure: PostWebMessageAsString to splash failed: " + ex.Message);
			}
		}

		private void ShowError(string message)
		{
			UnifiedLogger.Log("Launcher ERROR", message);
			PostStatus("[ERROR] " + message);

			MessageBox.Show(message, "Launcher Error",
				MessageBoxButtons.OK, MessageBoxIcon.Error);
		}
	}
}
