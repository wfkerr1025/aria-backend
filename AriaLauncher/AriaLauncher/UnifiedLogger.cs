using System;
using System.IO;

namespace AriaLauncher
{
	public static class UnifiedLogger
	{
		// Hard-anchor the repo root for reliability
		private static readonly string RepoRoot =
			@"D:\Users\William\ARIA-Lite Development\ARIA-Lite";

		private static readonly string LogDir =
			Path.Combine(RepoRoot, "logs");

		private static readonly string LogPath =
			Path.Combine(LogDir, "launcher.log");

		private static readonly object _lock = new();

		static UnifiedLogger()
		{
			Directory.CreateDirectory(LogDir);
		}

		public static void Log(string subsystem, string message)
		{
			lock (_lock)
			{
				File.AppendAllText(LogPath,
					$"[{DateTime.Now:HH:mm:ss.fff}] [{subsystem}] {message}\n");
			}
		}

		public static void LogError(string subsystem, string message, Exception ex)
		{
			lock (_lock)
			{
				File.AppendAllText(LogPath,
					$"[{DateTime.Now:HH:mm:ss.fff}] [{subsystem} ERROR] {message}\n{ex}\n");
			}
		}

		public static void LogException(string subsystem, Exception ex)
		{
			lock (_lock)
			{
				File.AppendAllText(LogPath,
					$"[{DateTime.Now:HH:mm:ss.fff}] [{subsystem} EXCEPTION] {ex}\n");
			}
		}
	}
}
