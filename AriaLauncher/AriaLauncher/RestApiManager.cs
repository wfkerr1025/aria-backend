using System;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Net.NetworkInformation;
using System.Threading.Tasks;

namespace AriaLauncher
{
	/// <summary>
	/// Owns the backend/rest/server.py process (the new /v1 REST API,
	/// separate from both the WebSocket backend BackendManager.cs starts
	/// and the existing backend/server.py FastAPI app on port 5000, which
	/// nothing in this launcher starts at all today). Split out as its own
	/// manager rather than folded into BackendManager, matching this
	/// codebase's existing convention (see BackendManager.cs's own comment:
	/// "StartElectron() now lives in ElectronManager — BackendManager owns
	/// only the backend + logging-server processes.").
	///
	/// Deliberately best-effort / non-fatal everywhere: the REST API is a
	/// new, optional surface that nothing else in the running app depends
	/// on yet (the WebUI talks to the WebSocket backend, not this). A
	/// failure here must never block or break the existing WS backend +
	/// Electron launch sequence — see SplashForm.cs's RunLaunchSequence(),
	/// which starts this after the WS backend is already confirmed ready
	/// and never gates on (or aborts for) its outcome.
	/// </summary>
	public static class RestApiManager
	{
		private const string AriaRoot =
			@"D:\Users\William\ARIA-Lite Development\ARIA-Lite";

		private const string ReadyMarker = "REST_API_READY";

		// Distinct from BackendManager.CandidatePorts (8766-8770) — the WS
		// backend can itself fall back into that range if 8766 is taken
		// (see BackendManager.ChoosePort()), so this pool starts one port
		// later and is additionally filtered against whatever port the WS
		// backend actually selected (see ChooseRestPort()) to avoid ever
		// colliding with it.
		private static readonly int[] CandidatePorts = { 8767, 8781, 8782, 8783, 8784 };
		public static int SelectedPort { get; private set; } = 8767;

		public static int ChooseRestPort(int avoidPort)
		{
			UnifiedLogger.Log("RestApi", "Choosing REST API port (avoiding " + avoidPort + ")...");

			try
			{
				var ipProps = IPGlobalProperties.GetIPGlobalProperties();
				var listeners = ipProps.GetActiveTcpListeners()
									   .Select(ep => ep.Port)
									   .ToHashSet();

				foreach (var port in CandidatePorts)
				{
					if (port == avoidPort)
					{
						UnifiedLogger.Log("RestApi", $"Port {port} is the WS backend's port, skipping.");
						continue;
					}
					if (listeners.Contains(port))
					{
						UnifiedLogger.Log("RestApi", $"Port {port} is in use, skipping.");
						continue;
					}

					SelectedPort = port;
					UnifiedLogger.Log("RestApi", $"Selected free port {port}");
					return port;
				}

				UnifiedLogger.Log("RestApi ERROR", "No free ports in the REST API candidate pool — falling back to 8767 anyway.");
				SelectedPort = 8767;
				return SelectedPort;
			}
			catch (Exception ex)
			{
				UnifiedLogger.LogError("RestApi", "ChooseRestPort() failed", ex);
				SelectedPort = 8767;
				return SelectedPort;
			}
		}

		public static Process? StartRestApi(int wsPort)
		{
			UnifiedLogger.Log("RestApi", "=== START REST API ===");
			UnifiedRemoteLogger.Log("RestApi", "INFO", "Starting REST API server (backend/rest/server.py)");

			int port = ChooseRestPort(wsPort);

			string restScript = Path.Combine(AriaRoot, "backend", "rest", "server.py");

			if (!File.Exists(restScript))
			{
				UnifiedLogger.Log("RestApi ERROR", "REST API script not found: " + restScript);
				UnifiedRemoteLogger.Log("RestApi", "ERROR", "REST API script not found: " + restScript);
				return null;
			}

			var psi = new ProcessStartInfo
			{
				FileName = "py",
				Arguments = $"\"{restScript}\" --port {port}",
				WorkingDirectory = Path.Combine(AriaRoot, "backend"),
				UseShellExecute = false,
				RedirectStandardOutput = true,
				RedirectStandardError = true,
				CreateNoWindow = true
			};

			var proc = new Process { StartInfo = psi, EnableRaisingEvents = true };

			proc.OutputDataReceived += (s, e) =>
			{
				if (!string.IsNullOrEmpty(e.Data))
					UnifiedLogger.Log("RestApi STDOUT", e.Data);
			};

			proc.ErrorDataReceived += (s, e) =>
			{
				if (!string.IsNullOrEmpty(e.Data))
					UnifiedLogger.Log("RestApi STDERR", e.Data);
			};

			try
			{
				if (!proc.Start())
				{
					UnifiedLogger.Log("RestApi ERROR", "Failed to start REST API process.");
					UnifiedRemoteLogger.Log("RestApi", "ERROR", "Failed to start REST API process.");
					return null;
				}

				proc.BeginOutputReadLine();
				proc.BeginErrorReadLine();

				proc.Exited += (s, e) =>
				{
					int code = proc.ExitCode;
					// Best-effort surface only — never fatal to the app, unlike
					// BackendManager's identical-looking handler for the WS backend.
					UnifiedLogger.Log(code == 0 ? "RestApi" : "RestApi WARN", "REST API process exited, code = " + code);
					UnifiedRemoteLogger.Log("RestApi", code == 0 ? "INFO" : "WARNING",
						"REST API process exited, code = " + code);
				};

				UnifiedLogger.Log("RestApi", "REST API started, PID = " + proc.Id);
				UnifiedRemoteLogger.Log("RestApi", "INFO", "REST API started, PID = " + proc.Id);
				return proc;
			}
			catch (Exception ex)
			{
				UnifiedLogger.LogError("RestApi", "Exception starting REST API", ex);
				UnifiedRemoteLogger.LogException("RestApi", ex);
				return null;
			}
		}

		public static async Task<bool> WaitForRestApiReady(Process restApi, int timeoutMs = 10000)
		{
			bool ready = false;

			restApi.OutputDataReceived += (s, e) =>
			{
				if (!string.IsNullOrEmpty(e.Data) && e.Data.Contains(ReadyMarker))
				{
					UnifiedLogger.Log("RestApi", "Detected REST API READY token.");
					ready = true;
				}
			};

			int elapsed = 0;
			while (!ready && elapsed < timeoutMs)
			{
				await Task.Delay(100);
				elapsed += 100;

				if (restApi.HasExited)
				{
					UnifiedLogger.Log("RestApi WARN", "REST API process exited before READY.");
					return false;
				}
			}

			if (!ready)
				UnifiedLogger.Log("RestApi WARN", "REST API did not signal READY within timeout — continuing without it (non-fatal).");

			return ready;
		}

		public static void ShutdownRestApi(Process? restApi)
		{
			UnifiedLogger.Log("RestApi", "=== SHUTDOWN REST API ===");

			if (restApi == null)
			{
				UnifiedLogger.Log("RestApi", "ShutdownRestApi called with null process.");
				return;
			}

			try
			{
				if (!restApi.HasExited)
				{
					UnifiedLogger.Log("RestApi", "Killing REST API PID " + restApi.Id);
					UnifiedRemoteLogger.Log("RestApi", "INFO", "Killing REST API PID " + restApi.Id);
					restApi.Kill(true);
				}
				else
				{
					UnifiedLogger.Log("RestApi", "REST API already exited.");
				}
			}
			catch (Exception ex)
			{
				UnifiedLogger.LogError("RestApi", "Exception killing REST API", ex);
				UnifiedRemoteLogger.LogException("RestApi", ex);
			}
		}
	}
}
