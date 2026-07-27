class ContractTests:
    def __init__(self, app):
        self.app = app

    def run(self):
        from backend.backend_contract import verify_backend_module

        ok = verify_backend_module(self.app, self.app.backend, "BackendAdapter")
        return {
            "success": ok,
            "message": "BackendAdapter contract verified" if ok else "Contract violation"
        }
