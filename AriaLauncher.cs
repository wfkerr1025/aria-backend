using System;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading.Tasks;

namespace AriaLauncher
{
	internal static class Program
	{
		// Adjust these paths if needed
		private static readonly string AriaRoot =
			@"D:\Users\William\ARIA-Lite Development\ARIA-Lite";

		private static readonly string AriaDesktopRoot =
			@"D:\Users\William\ARIA-Lite Development\ARIA-Lite Desktop";

		private static readonly string PythonExe =
			@"py"; // or full path to embedded python.exe

		private static readonly string BackendScript =
			Path.Combine(AriaRoot, @"backend\ws_server.py");

		private const string BackendReadyMarker = "WS_SERVER_READY";

		private static Process? backendProcess;
		private static Process? electronProcess;

		// -------------------------------
		// Steam-Style Job Object
		// -------------------------------
		private class JobObject : IDisposable
		{
			[DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
			private static extern IntPtr CreateJobObject(IntPtr lpJobAttributes, string lpName);

			[DllImport("kernel32.dll")]
			private static extern bool SetInformationJobObject(
				IntPtr hJob,
				JobObjectInfoType infoType,
				IntPtr lpJobObjectInfo,
				uint cbJobObjectInfoLength);

			[DllImport("kernel32.dll", SetLastError = true)]
			private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);

			[DllImport("kernel32.dll")]
			private static extern bool CloseHandle(IntPtr hObject);

			private IntPtr _jobHandle;

			public JobObject()
			{
				_jobHandle = CreateJobObject(IntPtr.Zero, null);

				var info = new JOBOBJECT_EXTENDED_LIMIT_INFORMATION();
				info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;

				int length = Marshal.SizeOf(typeof(JOBOBJECT_EXTENDED_LIMIT_INFORMATION));
				IntPtr ptr = Marshal.AllocHGlobal(length);
				Marshal.StructureToPtr(info, ptr, false);

				SetInformationJobObject(
					_jobHandle,
					JobObjectInfoType.ExtendedLimitInformation,
					ptr,
					(uint)length);

				Marshal.FreeHGlobal(ptr);
			}

			public void AddProcess(Process process)
			{
				AssignProcessToJobObject(_jobHandle, process.Handle);
			}

			public void Dispose()
			{
				if (_jobHandle != IntPtr.Zero)
				{
					CloseHandle(_jobHandle);
					_jobHandle = IntPtr.Zero;
				}
			}

			private const int JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000;

			private enum JobObjectInfoType
			{
				ExtendedLimitInformation = 9
			}

			[StructLayout(LayoutKind.Sequential)]
			private struct JOBOBJECT_BASIC_LIMIT_INFORMATION
			{
				public long PerProcessUserTimeLimit;
				public long PerJobUserTimeLimit;
				public int LimitFlags;
				public UIntPtr MinimumWorkingSetSize;
				public UIntPtr MaximumWorkingSetSize;
				public int ActiveProcessLimit;
				public long Affinity;
				public int PriorityClass;
				public int SchedulingClass;
			}

			[StructLayout(LayoutKind.Sequential)]
			private struct IO_COUNTERS
			{
				public ulong ReadOperationCount;
				public ulong WriteOperationCount;
				public ulong OtherOperationCount;
				public ulong ReadTransferCount;
				public ulong WriteTransferCount;
				public ulong OtherTransferCount;
			}

			[StructLayout(LayoutKind.Sequential)]
			private struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION
			{
				public JOBOBJECT_BASIC_LIMIT_INFORMATION BasicLimitInformation;
				public IO_COUNTERS IoInfo;
				public UIntPtr ProcessMemoryLimit;
				public UIntPtr JobMemoryLimit;
				public UIntPtr PeakProcessMemoryUsed;
				public UIntPtr PeakJobMemoryUsed;
			}
		}

		// -------------------------------
		// Main Launcher
		// -------------------------------
		[STAThread]
		private static async Task Main()
		{
			Console.OutputEncoding = Encoding.UTF8;

			if (!File.Exists(BackendScript))
			{
				Console.WriteLine("[Launcher] Backend script not found: " + BackendScript);
				return;
			}

			Console.WriteLine("[Launcher] ARIA Root: " + AriaRoot);
			Console.WriteLine("[Launcher] Desktop Root: " + AriaDesktopRoot);

			using (var job = new JobObject())
			{
				// 1. Start backend
				backendProcess = StartBackend();

				if (backendProcess == null)
				{
					Console.WriteLine("[Launcher] Failed to start backend.");
					return;
				}

				job.AddProcess(backendProcess);

				// 2. Wait for WS_SERVER_READY
				bool ready = await WaitForBackendReady(backendProcess);

				if (!ready)
				{
					Console.WriteLine("[Launcher] Backend did not signal readiness.");
					return;
				}

				Console.WriteLine("[Launcher] Backend ready. Launching Electron...");

				// 3. Start Electron
				electronProcess = StartElectron();

				if (electronProcess == null)
				{
					Console.WriteLine("[Launcher] Failed to start Electron.");
					return;
				}

				job.AddProcess(electronProcess);

				// 4. Auto-exit when Electron closes
				electronProcess.WaitForExit();
				Console.WriteLine("[Launcher] Electron exited with code " + electronProcess.ExitCode);

				// JobObject.Dispose() will kill backend + Electron automatically
			}
		}

		private static Process? StartBackend()
		{
			try
			{
				var psi = new ProcessStartInfo
				{
					FileName = PythonExe,
					Arguments = $"\"{BackendScript}\"",
					WorkingDirectory = AriaRoot,
					UseShellExecute = false,
					RedirectStandardOutput = true,
					RedirectStandardError = true,
					CreateNoWindow = true
				};

				var proc = new Process { StartInfo = psi };
				proc.OutputDataReceived += (s, e) =>
				{
					if (!string.IsNullOrEmpty(e.Data))
						Console.WriteLine("[Backend] " + e.Data);
				};
				proc.ErrorDataReceived += (s, e) =>
				{
					if (!string.IsNullOrEmpty(e.Data))
						Console.WriteLine("[Backend ERR] " + e.Data);
				};

				if (!proc.Start())
					return null;

				proc.BeginOutputReadLine();
				proc.BeginErrorReadLine();

				Console.WriteLine("[Launcher] Backend process started. PID: " + proc.Id);
				return proc;
			}
			catch (Exception ex)
			{
				Console.WriteLine("[Launcher] Failed to start backend: " + ex.Message);
				return null;
			}
		}

		private static async Task<bool> WaitForBackendReady(Process backend)
		{
			var tcs = new TaskCompletionSource<bool>();

			DataReceivedEventHandler? handler = null;
			handler = (s, e) =>
			{
				if (string.IsNullOrEmpty(e.Data))
					return;

				if (e.Data.Contains(BackendReadyMarker))
				{
					Console.WriteLine("[Launcher] Detected backend readiness marker.");
					tcs.TrySetResult(true);
				}
			};

			backend.OutputDataReceived += handler;

			var timeoutTask = Task.Delay(TimeSpan.FromSeconds(30));
			var readyTask = tcs.Task;

			var completed = await Task.WhenAny(readyTask, timeoutTask);

			backend.OutputDataReceived -= handler;

			if (completed == timeoutTask)
			{
				Console.WriteLine("[Launcher] Timeout waiting for backend readiness.");
				return false;
			}

			return readyTask.Result;
		}

		private static Process? StartElectron()
		{
			try
			{
				var psi = new ProcessStartInfo
				{
					FileName = Path.Combine(AriaDesktopRoot, "runtime", "node", "npx.cmd"),
					Arguments = "electron .",
					WorkingDirectory = AriaDesktopRoot,
					UseShellExecute = false,
					RedirectStandardOutput = true,
					RedirectStandardError = true,
					CreateNoWindow = false
				};

				// Ensure npx resolves local node_modules
				psi.EnvironmentVariables["NODE_PATH"] = Path.Combine(AriaDesktopRoot, "node_modules");

				var proc = new Process { StartInfo = psi };
				proc.OutputDataReceived += (s, e) =>
				{
					if (!string.IsNullOrEmpty(e.Data))
						Console.WriteLine("[Electron] " + e.Data);
				};
				proc.ErrorDataReceived += (s, e) =>
				{
					if (!string.IsNullOrEmpty(e.Data))
						Console.WriteLine("[Electron ERR] " + e.Data);
				};

				if (!proc.Start())
					return null;

				proc.BeginOutputReadLine();
				proc.BeginErrorReadLine();

				Console.WriteLine("[Launcher] Electron process started. PID: " + proc.Id);
				return proc;
			}
			catch (Exception ex)
			{
				Console.WriteLine("[Launcher] Failed to start Electron: " + ex.Message);
				return null;
			}
		}
	}
}
