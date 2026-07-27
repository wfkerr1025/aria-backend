class TestRegistry:
    """
    Central registry for backend subsystem tests.
    Each test returns:
        {
            "success": bool,
            "message": str
        }
    """

    def __init__(self, app):
        self.app = app

        from backend.tests.workspace_tests import WorkspaceTests
        from backend.tests.sandbox_tests import SandboxTests
        from backend.tests.toolchain_tests import ToolchainTests
        from backend.tests.contract_tests import ContractTests
        from backend.tests.router_tests import RouterTests

        self.tests = {
            "Workspace API": WorkspaceTests(app),
            "Script Sandbox": SandboxTests(app),
            "Toolchain Registry": ToolchainTests(app),
            "Backend Contract": ContractTests(app),
            "Router Health": RouterTests(app),
        }

    def run_all(self):
        results = {}
        for name, test in self.tests.items():
            try:
                results[name] = test.run()
            except Exception as e:
                results[name] = {
                    "success": False,
                    "message": str(e),
                }
        return results
