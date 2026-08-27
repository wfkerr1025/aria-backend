using System;
using System.Windows.Forms;

namespace AriaLauncher
{
	internal static class Program
	{
		[STAThread]
		static void Main()
		{
			// --- Unified Startup Logging ---
			UnifiedLogger.Log("Launcher", "=== ARIA LAUNCHER STARTUP ===");
			UnifiedLogger.Log("Launcher", "Process started at: " + DateTime.Now);
			UnifiedLogger.Log("Launcher", "ApplicationConfiguration.Initialize()");

			try
			{
				// ⭐ FIX: Enable proper DPI scaling so WebView2 and the splash screen
				// render at the correct size on modern Windows displays.
				Application.SetHighDpiMode(HighDpiMode.PerMonitorV2);

				Application.EnableVisualStyles();
				Application.SetCompatibleTextRenderingDefault(false);

				ApplicationConfiguration.Initialize();
			}
			catch (Exception ex)
			{
				UnifiedLogger.LogError("Launcher", "ApplicationConfiguration failed", ex);
				UnifiedRemoteLogger.LogException("Launcher", ex);
				MessageBox.Show("Launcher failed during initialization.\n\n" + ex.Message,
					"Launcher Error", MessageBoxButtons.OK, MessageBoxIcon.Error);
				return;
			}

			UnifiedLogger.Log("Launcher", "Creating SplashForm...");

			SplashForm splash = null;

			try
			{
				splash = new SplashForm();
				UnifiedLogger.Log("Launcher", "SplashForm created successfully.");
			}
			catch (Exception ex)
			{
				UnifiedLogger.LogError("Launcher", "SplashForm constructor failed", ex);
				UnifiedRemoteLogger.LogException("Launcher", ex);
				MessageBox.Show("Failed to create splash window.\n\n" + ex.Message,
					"Launcher Error", MessageBoxButtons.OK, MessageBoxIcon.Error);
				return;
			}

			UnifiedLogger.Log("Launcher", "Running SplashForm...");
			UnifiedLogger.Log("Launcher", "=== ENTERING MESSAGE LOOP ===");

			try
			{
				Application.Run(splash);
			}
			catch (Exception ex)
			{
				UnifiedLogger.LogError("Launcher", "Application.Run crashed", ex);
				UnifiedRemoteLogger.LogException("Launcher", ex);
				MessageBox.Show("Launcher encountered a fatal error.\n\n" + ex.Message,
					"Launcher Error", MessageBoxButtons.OK, MessageBoxIcon.Error);
			}

			UnifiedLogger.Log("Launcher", "=== ARIA LAUNCHER EXIT ===");
			UnifiedRemoteLogger.Log("Launcher", "INFO", "=== ARIA LAUNCHER EXIT ===");
		}
	}
}
