class SandboxTests:
    def __init__(self, app):
        self.app = app

    def run(self):
        from backend.sandbox.python_executor import PythonExecutor

        executor = PythonExecutor(self.app)
        result = executor.run_inline("print('sandbox ok')")

        success = result.get("stdout", "").strip() == "sandbox ok"
        return {
            "success": success,
            "message": result.get("stdout", "").strip()
        }
