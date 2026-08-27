using System;
using System.Collections.Concurrent;
using System.Net.Http;
using System.Text;
using System.Text.Json;
using System.Threading;

namespace AriaLauncher
{
	/// <summary>
	/// Client for the unified cross-runtime logging server
	/// (backend/logging_server.py, http://127.0.0.1:5001/log).
	///
	/// Distinct from UnifiedLogger (which writes the launcher's own local
	/// logs/launcher.log and works even before the log server exists —
	/// useful during the very first bootstrap steps). This one ships every
	/// call to the ONE shared run-log file that every runtime in the stack
	/// (this launcher, the Python backend, Electron, the webui) writes
	/// into, interleaved in one timeline.
	///
	/// Log()/LogException() must never be able to break the caller: they
	/// queue the entry and return immediately, and the background sender
	/// swallows every failure (log server not started yet, down, network
	/// hiccup, ...).
	/// </summary>
	public static class UnifiedRemoteLogger
	{
		private const string LogServerUrl = "http://127.0.0.1:5001/log";

		private static readonly HttpClient _http = new HttpClient
		{
			Timeout = TimeSpan.FromSeconds(2)
		};

		private static readonly BlockingCollection<string> _queue =
			new BlockingCollection<string>(new ConcurrentQueue<string>(), 5000);

		private static readonly Thread _worker;

		static UnifiedRemoteLogger()
		{
			_worker = new Thread(WorkerLoop)
			{
				IsBackground = true,
				Name = "aria-unified-remote-logger"
			};
			_worker.Start();
		}

		public static void Log(string subsystem, string level, string message)
		{
			Enqueue(subsystem, level, message, null);
		}

		public static void LogException(string subsystem, Exception ex)
		{
			Enqueue(subsystem, "ERROR", ex.Message, ex.ToString());
		}

		private static void Enqueue(string subsystem, string level, string message, string? context)
		{
			try
			{
				var payload = new
				{
					subsystem,
					level,
					message,
					context = context == null ? null : new { trace = context }
				};

				string json = JsonSerializer.Serialize(payload);

				// TryAdd (not Add) so a full queue drops the entry instead of
				// blocking the caller — logging must never stall the launcher.
				_queue.TryAdd(json, 0);
			}
			catch
			{
				// Logging must never break runtime.
			}
		}

		private static void WorkerLoop()
		{
			foreach (var json in _queue.GetConsumingEnumerable())
			{
				try
				{
					var content = new StringContent(json, Encoding.UTF8, "application/json");
					// Fire-and-forget, but synchronously on this dedicated
					// background thread so entries stay ordered and the
					// caller's thread (often the WinForms UI thread) never
					// blocks on network I/O.
					_http.PostAsync(LogServerUrl, content).GetAwaiter().GetResult();
				}
				catch
				{
					// Log server may not be up yet, may be down, or the
					// network hiccuped — drop and move on.
				}
			}
		}
	}
}
