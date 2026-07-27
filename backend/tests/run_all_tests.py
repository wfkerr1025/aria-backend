import subprocess
import os

TESTS = [
    "workspace_tests.py",
    "sandbox_tests.py",
    "router_tests.py",
    "contract_tests.py",
    "test_registry.py",
    "toolchain_tests.py",
]

for test in TESTS:
    path = os.path.join(os.path.dirname(__file__), test)
    print(f"Running {test}...")
    result = subprocess.run(["py", path])
    if result.returncode != 0:
        print(f"{test} FAILED")
        exit(result.returncode)

print("All tests passed.")
