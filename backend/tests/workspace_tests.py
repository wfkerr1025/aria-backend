class WorkspaceTests:
    def __init__(self, app):
        self.app = app

    def run(self):
        from backend.workspace.project_manager import ProjectManager

        pm = ProjectManager(self.app)

        try:
            pm.create_project("diag_test")
            pm.add_file_to_project("diag_test", "hello.txt", "Hello ARIA")
            content = pm.read_project_file("diag_test", "hello.txt")
            pm.delete_project("diag_test")

            return {
                "success": content == "Hello ARIA",
                "message": f"Read content: {content}"
            }

        except Exception as e:
            return {"success": False, "message": str(e)}
