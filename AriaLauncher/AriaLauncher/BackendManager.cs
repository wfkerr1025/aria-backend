using System;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Net.NetworkInformation;
using System.Threading.Tasks;

namespace AriaLauncher
{
	public static class BackendManager
	{
		// Explicit roots – adjust if you move the projects
		private const string AriaRoot =
			@"D:\Users\William\ARIA-Lite Development\ARIA-Lite";
		private const string DesktopRoot =
			@"D:\Users\William\ARIA-Lite Development\ARIA-Lite Desktop";

		private const string LoggingServerReadyMarker = "LOG_SERVER_READY";
		private static Process? loggingServerProcess;

		private static readonly int[] CandidatePorts = { 8766, 8767, 8768, 8769, 8770 };
		public static int SelectedPort { get; private set; } = 8766;

		// -------------------------------------------------------------
		// Unified logging server — must start FIRST, before the backend
		// and Electron, so every later process has somewhere to log to
		// and the whole run ends up in one ARIA_Run_*.log file.
		// -------------------------------------------------------------
		public static Process? StartLoggingServer()
		{
			UnifiedLogger.Log("LogServer", "=== START LOGGING SERVER ===");

			string loggingScript = Path.Combine(AriaRoot, "backend", "logging_server.py");

			if (!File.Exists(loggingScript))
			{
				UnifiedLogger.Log("LogServer ERROR", "logging_server.py not found: " + loggingScript);
				return null;
			}

			var psi = new ProcessStartInfo
			{
				FileName = "py",
				Arguments = $"\"{loggingScript}\"",
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
					UnifiedLogger.Log("LogServer STDOUT", e.Data);
			};

			proc.ErrorDataReceived += (s, e) =>
			{
				if (!string.IsNullOrEmpty(e.Data))
					UnifiedLogger.Log("LogServer STDERR", e.Data);
			};

			try
			{
				if (!proc.Start())
				{
					UnifiedLogger.Log("LogServer ERROR", "Failed to start logging server process.");
					return null;
				}

				proc.BeginOutputReadLine();
				proc.BeginErrorReadLine();

				UnifiedLogger.Log("LogServer", "Logging server started, PID = " + proc.Id);
				loggingServerProcess = proc;
				return proc;
			}
			catch (Exception ex)
			{
				UnifiedLogger.LogError("LogServer", "Exception starting logging server", ex);
				return null;
			}
		}

		public static async Task<bool> WaitForLoggingServerReady(Process loggingServer, int timeoutMs = 15000)
		{
			var tcs = new TaskCompletionSource<bool>();

			DataReceivedEventHandler? handler = null;
			handler = (s, e) =>
			{
				if (!string.IsNullOrEmpty(e.Data) && e.Data.Contains(LoggingServerReadyMarker))
				{
					UnifiedLogger.Log("LogServer", "Detected logging server READY token.");
					tcs.TrySetResult(true);
				}
			};

			loggingServer.OutputDataReceived += handler;

			var timeoutTask = Task.Delay(timeoutMs);
			var completed = await Task.WhenAny(tcs.Task, timeoutTask);

			loggingServer.OutputDataReceived -= handler;

			if (completed == timeoutTask)
			{
				UnifiedLogger.Log("LogServer WARN", "Timeout waiting for logging server readiness.");
				return false;
			}

			// Now that the log server is up, remote logging can start flowing.
			UnifiedRemoteLogger.Log("Launcher", "INFO", "Logging server ready — remote logging active.");
			return tcs.Task.Result;
		}

		public static void ShutdownLoggingServer()
		{
			if (loggingServerProcess == null)
				return;

			try
			{
				if (!loggingServerProcess.HasExited)
				{
					UnifiedLogger.Log("LogServer", "Killing logging server PID " + loggingServerProcess.Id);
					loggingServerProcess.Kill(true);
				}
			}
			catch (Exception ex)
			{
				UnifiedLogger.LogError("LogServer", "Exception killing logging server", ex);
			}
		}

		public static int ChoosePort()
		{
			UnifiedLogger.Log("Backend", "Choosing backend port...");

			try
			{
				var ipProps = IPGlobalProperties.GetIPGlobalProperties();
				var listeners = ipProps.GetActiveTcpListeners()
									   .Select(ep => ep.Port)
									   .ToHashSet();

				UnifiedLogger.Log("Backend", "Active listeners: " +
					string.Join(", ", listeners));

				foreach (var port in CandidatePorts)
				{
					if (!listeners.Contains(port))
					{
						SelectedPort = port;
						UnifiedLogger.Log("Backend", $"Selected free port {port}");
						return port;
					}

					UnifiedLogger.Log("Backend", $"Port {port} is in use, skipping.");
				}

				UnifiedLogger.Log("Backend ERROR", "No free ports in range 8766–8770.");
				SelectedPort = 8766;
				return SelectedPort;
			}
			catch (Exception ex)
			{
				UnifiedLogger.LogError("Backend", "ChoosePort() failed", ex);
				SelectedPort = 8766;
				return SelectedPort;
			}
		}

		public static Process? StartBackend()
		{
			UnifiedLogger.Log("Backend", "=== START BACKEND ===");
			UnifiedRemoteLogger.Log("Backend", "INFO", "Starting Python backend (ws_server.py)");

			int port = ChoosePort();

			string backendScript = Path.Combine(AriaRoot, "backend", "ws_server.py");

			UnifiedLogger.Log("Backend", "Backend script path: " + backendScript);

			if (!File.Exists(backendScript))
			{
				UnifiedLogger.Log("Backend ERROR", "Backend script not found: " + backendScript);
				UnifiedRemoteLogger.Log("Backend", "ERROR", "Backend script not found: " + backendScript);
				return null;
			}

			var psi = new ProcessStartInfo
			{
				FileName = "py",
				Arguments = $"\"{backendScript}\" --port {port}",
				WorkingDirectory = Path.Combine(AriaRoot, "backend"),
				UseShellExecute = false,
				RedirectStandardOutput = true,
				RedirectStandardError = true,
				CreateNoWindow = true
			};

			UnifiedLogger.Log("Backend", "Launching backend process:");
			UnifiedLogger.Log("Backend", "  FileName  = " + psi.FileName);
			UnifiedLogger.Log("Backend", "  Arguments = " + psi.Arguments);
			UnifiedLogger.Log("Backend", "  WorkDir   = " + psi.WorkingDirectory);

			var proc = new Process { StartInfo = psi, EnableRaisingEvents = true };

			proc.OutputDataReceived += (s, e) =>
			{
				if (!string.IsNullOrEmpty(e.Data))
					UnifiedLogger.Log("Backend STDOUT", e.Data);
			};

			proc.ErrorDataReceived += (s, e) =>
			{
				if (!string.IsNullOrEmpty(e.Data))
					UnifiedLogger.Log("Backend STDERR", e.Data);
			};

			try
			{
				if (!proc.Start())
				{
					UnifiedLogger.Log("Backend ERROR", "Failed to start backend process.");
					UnifiedRemoteLogger.Log("Backend", "ERROR", "Failed to start backend process.");
					return null;
				}

				proc.BeginOutputReadLine();
				proc.BeginErrorReadLine();

				proc.Exited += (s, e) =>
				{
					// Fires on any exit, intentional (ShutdownBackend) or not —
					// code == 0 covers the normal case, anything else is a crash.
					int code = proc.ExitCode;
					UnifiedLogger.Log(code == 0 ? "Backend" : "Backend ERROR", "Backend process exited, code = " + code);
					UnifiedRemoteLogger.Log("Backend", code == 0 ? "INFO" : "ERROR",
						"Backend process exited, code = " + code);
				};

				UnifiedLogger.Log("Backend", "Backend started, PID = " + proc.Id);
				UnifiedRemoteLogger.Log("Backend", "INFO", "Backend started, PID = " + proc.Id);
				return proc;
			}
			catch (Exception ex)
			{
				UnifiedLogger.LogError("Backend", "Exception starting backend", ex);
				UnifiedRemoteLogger.LogException("Backend", ex);
				return null;
			}
		}

		public static void ShutdownBackend(Process? backend)
		{
			UnifiedLogger.Log("Backend", "=== SHUTDOWN BACKEND ===");

			if (backend == null)
			{
				UnifiedLogger.Log("Backend", "ShutdownBackend called with null process.");
				return;
			}

			try
			{
				if (!backend.HasExited)
				{
					UnifiedLogger.Log("Backend", "Killing backend PID " + backend.Id);
					UnifiedRemoteLogger.Log("Backend", "INFO", "Killing backend PID " + backend.Id);
					backend.Kill(true);
				}
				else
				{
					UnifiedLogger.Log("Backend", "Backend already exited.");
				}
			}
			catch (Exception ex)
			{
				UnifiedLogger.LogError("Backend", "Exception killing backend", ex);
				UnifiedRemoteLogger.LogException("Backend", ex);
			}
		}

		// StartElectron() now lives in ElectronManager — BackendManager owns
		// only the backend + logging-server processes.

		// -------------------------------------------------------------
		// Startup initialization pipeline — short-lived, run-to-completion
		// steps (unlike StartBackend()/StartLoggingServer() above, which
		// start a long-running process this class manages the lifetime
		// of). Each step is its own `py -m backend.core.init_pipeline
		// --step <name>` process so SplashForm can show real per-step
		// progress text; see backend/core/init_pipeline.py's module
		// docstring for why each invocation re-runs prior steps rather
		// than trying to persist state across process boundaries.
		// -------------------------------------------------------------
		private static async Task<bool> RunPipelineStepAsync(string stepName, string friendlyName)
		{
			UnifiedLogger.Log("InitPipeline", $"=== RUN STEP: {stepName} ===");

			var psi = new ProcessStartInfo
			{
				FileName = "py",
				Arguments = $"-m backend.core.init_pipeline --step {stepName}",
				WorkingDirectory = AriaRoot,
				UseShellExecute = false,
				RedirectStandardOutput = true,
				RedirectStandardError = true,
				CreateNoWindow = true
			};

			using var proc = new Process { StartInfo = psi, EnableRaisingEvents = true };

			proc.OutputDataReceived += (s, e) =>
			{
				if (!string.IsNullOrEmpty(e.Data))
					UnifiedLogger.Log("InitPipeline STDOUT", e.Data);
			};

			proc.ErrorDataReceived += (s, e) =>
			{
				if (!string.IsNullOrEmpty(e.Data))
					UnifiedLogger.Log("InitPipeline STDERR", e.Data);
			};

			try
			{
				if (!proc.Start())
				{
					UnifiedLogger.Log("InitPipeline ERROR", $"Failed to start {friendlyName} process.");
					return false;
				}

				proc.BeginOutputReadLine();
				proc.BeginErrorReadLine();

				await proc.WaitForExitAsync();

				bool ok = proc.ExitCode == 0;
				UnifiedLogger.Log(ok ? "InitPipeline" : "InitPipeline ERROR",
					$"{friendlyName} finished, exit code = {proc.ExitCode}");
				return ok;
			}
			catch (Exception ex)
			{
				UnifiedLogger.LogError("InitPipeline", $"Exception running {friendlyName}", ex);
				UnifiedRemoteLogger.LogException("InitPipeline", ex);
				return false;
			}
		}

		public static Task<bool> RunModelScanAndCache() => RunPipelineStepAsync("scan", "Model scan");
		public static Task<bool> RunGGUFMetadataPass() => RunPipelineStepAsync("metadata", "GGUF metadata pass");
		public static Task<bool> RunRequirementDerivation() => RunPipelineStepAsync("requirements", "Requirement derivation");
		public static Task<bool> WriteRequirementCache() => RunPipelineStepAsync("cache", "Requirement cache write");
	}
}
