class ToolchainTests:
    def __init__(self, app):
        self.app = app

    def run(self):
        from backend.toolchain.registry import ToolchainRegistry

        registry = ToolchainRegistry(self.app)
        result = registry.git().run(["--version"])

        success = result["exit_code"] == 0
        return {
            "success": success,
            "message": result["stdout"] or result["stderr"]
        }
