import time
import traceback

def startup_selftest(app=None):
    results = {
        "timestamp": time.ctime(),
        "status": "ok",
        "sections": {},
        "boot_log": "",
    }

    boot_log_lines = []
    def log(msg): boot_log_lines.append(msg)

    # BACKEND HEALTH
    backend_status = {"status": "unknown", "latency_ms": None, "error": None, "response": None}
    try:
        log("[SelfTest] Checking backend /health")
        start = time.time()
        reply = app.backend.send("/health") if app and hasattr(app, "backend") else {"status": "no-backend"}
        backend_status["latency_ms"] = round((time.time() - start) * 1000, 2)
        backend_status["response"] = reply
        backend_status["status"] = "ok" if reply else "no-response"
    except Exception as e:
        backend_status["status"] = "error"
        backend_status["error"] = str(e)
        backend_status["traceback"] = traceback.format_exc()
    results["sections"]["backend_health"] = backend_status

    # ROUTER
    router_info = {"status": "unknown", "routes": None, "error": None}
    try:
        log("[SelfTest] Fetching backend router map")
        router = app.backend.router_map() if app and hasattr(app, "backend") else {}
        router_info["routes"] = router
        router_info["status"] = "ok"
    except Exception as e:
        router_info["status"] = "error"
        router_info["error"] = str(e)
        router_info["traceback"] = traceback.format_exc()
    results["sections"]["router"] = router_info

    # METADATA
    metadata_info = {"status": "unknown", "metadata": None, "error": None}
    try:
        log("[SelfTest] Fetching backend metadata")
        meta = app.backend.send("/metadata") if app and hasattr(app, "backend") else {}
        metadata_info["metadata"] = meta
        metadata_info["status"] = "ok"
    except Exception as e:
        metadata_info["status"] = "error"
        metadata_info["error"] = str(e)
        metadata_info["traceback"] = traceback.format_exc()
    results["sections"]["metadata"] = metadata_info

    # CONTEXT
    context_info = {"status": "unknown", "context": None, "error": None}
    try:
        log("[SelfTest] Fetching backend context")
        ctx = app.backend.send("/context") if app and hasattr(app, "backend") else {}
        context_info["context"] = ctx
        context_info["status"] = "ok"
    except Exception as e:
        context_info["status"] = "error"
        context_info["error"] = str(e)
        context_info["traceback"] = traceback.format_exc()
    results["sections"]["context"] = context_info

    # PING
    ping_info = {"status": "unknown", "latency_ms": None, "response": None, "error": None}
    try:
        log("[SelfTest] Checking backend /ping")
        start = time.time()
        reply = app.backend.send("/ping") if app and hasattr(app, "backend") else {"status": "no-backend"}
        ping_info["latency_ms"] = round((time.time() - start) * 1000, 2)
        ping_info["response"] = reply
        ping_info["status"] = "ok" if reply else "no-response"
    except Exception as e:
        ping_info["status"] = "error"
        ping_info["error"] = str(e)
        ping_info["traceback"] = traceback.format_exc()
    results["sections"]["ping"] = ping_info

    results["boot_log"] = "\n".join(boot_log_lines)
    return results
