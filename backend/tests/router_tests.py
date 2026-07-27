class RouterTests:
    def __init__(self, app):
        self.app = app

    def run(self):
        resp = self.app.backend.send("/health")

        success = resp.get("status") == "ok"
        return {
            "success": success,
            "message": str(resp)
        }
