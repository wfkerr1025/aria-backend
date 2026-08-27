using System;
using System.Diagnostics;
using System.IO;

namespace AriaLauncher
{
	/// <summary>
	/// Owns the Electron frontend process. Split out of BackendManager,
	/// which previously started both the Python backend and Electron —
	/// this keeps each manager scoped to one process it owns.
	/// </summary>
	public static class ElectronManager
	{
		private const string DesktopRoot =
			@"D:\Users\William\ARIA-Lite Development\ARIA-Lite Desktop";
		private const string AriaRoot =
			@"D:\Users\William\ARIA-Lite Development\ARIA-Lite";

		public static Process? StartElectron()
		{
			UnifiedLogger.Log("Electron", "=== START ELECTRON ===");
			UnifiedRemoteLogger.Log("Electron", "INFO", "Starting Electron frontend");

			string electronCmd = Path.Combine(
				DesktopRoot, "node_modules", ".bin", "electron.cmd");

			UnifiedLogger.Log("Electron", "Electron.cmd path: " + electronCmd);

			if (!File.Exists(electronCmd))
			{
				UnifiedLogger.Log("Electron ERROR", "electron.cmd not found: " + electronCmd);
				UnifiedRemoteLogger.Log("Electron", "ERROR", "electron.cmd not found: " + electronCmd);
				return null;
			}

			string mainJs = Path.Combine(DesktopRoot, "main.js");

			UnifiedLogger.Log("Electron", "main.js path: " + mainJs);

			if (!File.Exists(mainJs))
			{
				UnifiedLogger.Log("Electron ERROR", "main.js not found: " + mainJs);
				UnifiedRemoteLogger.Log("Electron", "ERROR", "main.js not found: " + mainJs);
				return null;
			}

			var psi = new ProcessStartInfo
			{
				FileName = electronCmd,
				Arguments = "main.js",
				WorkingDirectory = DesktopRoot,
				UseShellExecute = false,
				CreateNoWindow = false
			};

			psi.EnvironmentVariables["ARIA_ROOT"] = AriaRoot;
			psi.EnvironmentVariables["ARIA_WS_PORT"] = BackendManager.SelectedPort.ToString();

			UnifiedLogger.Log("Electron", "Environment variables:");
			UnifiedLogger.Log("Electron", "  ARIA_ROOT = " + psi.EnvironmentVariables["ARIA_ROOT"]);
			UnifiedLogger.Log("Electron", "  ARIA_WS_PORT = " + psi.EnvironmentVariables["ARIA_WS_PORT"]);

			try
			{
				var proc = Process.Start(psi);
				if (proc == null)
				{
					UnifiedLogger.Log("Electron ERROR", "Failed to start Electron process.");
					UnifiedRemoteLogger.Log("Electron", "ERROR", "Failed to start Electron process.");
					return null;
				}

				UnifiedLogger.Log("Electron", "Electron started, PID = " + proc.Id);
				UnifiedRemoteLogger.Log("Electron", "INFO", "Electron started, PID = " + proc.Id);
				return proc;
			}
			catch (Exception ex)
			{
				UnifiedLogger.LogError("Electron", "Exception starting Electron", ex);
				UnifiedRemoteLogger.LogException("Electron", ex);
				return null;
			}
		}

		public static void ShutdownElectron(Process? electron)
		{
			UnifiedLogger.Log("Electron", "=== SHUTDOWN ELECTRON ===");

			if (electron == null)
			{
				UnifiedLogger.Log("Electron", "ShutdownElectron called with null process.");
				return;
			}

			try
			{
				if (!electron.HasExited)
				{
					UnifiedLogger.Log("Electron", "Killing Electron PID " + electron.Id);
					UnifiedRemoteLogger.Log("Electron", "INFO", "Killing Electron PID " + electron.Id);
					electron.Kill(true);
				}
				else
				{
					UnifiedLogger.Log("Electron", "Electron already exited, code = " + electron.ExitCode);
					UnifiedRemoteLogger.Log("Electron", electron.ExitCode == 0 ? "INFO" : "ERROR",
						"Electron exited, code = " + electron.ExitCode);
				}
			}
			catch (Exception ex)
			{
				UnifiedLogger.LogError("Electron", "Exception killing Electron", ex);
				UnifiedRemoteLogger.LogException("Electron", ex);
			}
		}
	}
}
