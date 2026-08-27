# Execution Sandbox

Best-effort execution sandbox — CPU/time/memory limits for tool and
plugin execution (Phase 2, items 2 & 8).

Honest scope note: this is a Windows-developed, cross-platform-running
app, and neither `resource.setrlimit` (POSIX-only) nor Job Objects
(the real Windows mechanism for hard memory/CPU caps on a process) are
available from pure Python without a native extension this project
doesn't depend on. So:

  - the TIME limit is real and hard: run_in_sandbox() executes the
    callable on a worker thread and simply stops waiting at the
    timeout, returning a "timed out" SandboxResult. The callable's
    thread is NOT forcibly killed (Python has no safe API to kill an
    arbitrary thread) — it keeps running in the background, but the
    caller is unblocked and never sees output from it past the deadline.
  - the MEMORY limit is a monitor-and-report cap, not a hard kill: a
    watcher polls the current process's RSS via psutil while the
    callable runs and flags a violation if it grows past the budget.
    For first-party tool code (the only thing this sandbox runs today)
    that's sufficient to catch a runaway allocation; it is NOT isolation
    from a genuinely malicious plugin, which would need real process-
    level isolation (a subprocess sandbox) — see run_in_subprocess()
    below for that stronger, slower option when it's actually needed.
  - CPU is reported (time actually spent), not capped — a hard CPU-time
    cap has the same POSIX-only limitation as memory.

Every SandboxResult tells the caller exactly what was and wasn't
enforced, rather than silently pretending a soft cap was a hard one.

## Classes

### `SandboxLimits`

SandboxLimits(timeout_seconds: 'float' = 10.0, memory_limit_mb: 'float' = 512.0)

- `__init__(self, timeout_seconds: 'float' = 10.0, memory_limit_mb: 'float' = 512.0) -> None`
  Initialize self.  See help(type(self)) for accurate signature.

### `SandboxResult`

SandboxResult(ok: 'bool', value: 'Any' = None, error: 'Optional[str]' = None, timed_out: 'bool' = False, memory_exceeded: 'bool' = False, elapsed_seconds: 'float' = 0.0, peak_memory_mb: 'float' = 0.0, enforcement: 'dict' = <factory>)

- `__init__(self, ok: 'bool', value: 'Any' = None, error: 'Optional[str]' = None, timed_out: 'bool' = False, memory_exceeded: 'bool' = False, elapsed_seconds: 'float' = 0.0, peak_memory_mb: 'float' = 0.0, enforcement: 'dict' = <factory>) -> None`
  Initialize self.  See help(type(self)) for accurate signature.

## Functions

### `run_in_sandbox(fn: 'Callable[..., Any]', *args, limits: 'Optional[SandboxLimits]' = None, **kwargs) -> 'SandboxResult'`

Run fn(*args, **kwargs) on a worker thread with a hard wall-clock
timeout and best-effort memory monitoring. See module docstring for
exactly what "hard" and "best-effort" mean here.

### `run_in_subprocess(fn: 'Callable[..., Any]', *args, limits: 'Optional[SandboxLimits]' = None, **kwargs) -> 'SandboxResult'`

Stronger isolation than run_in_sandbox(): a genuinely separate OS
process, which CAN be forcibly terminated on timeout (unlike a
thread) — real isolation for a plugin/tool whose trustworthiness is
lower than "first-party code we wrote". fn must be picklable
(module-level, not a closure/lambda) since multiprocessing pickles
it to hand to the child process.
