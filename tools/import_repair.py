def scan_and_fix():
    module_map = find_all_modules()

    print("\n=== ARIA Lite Import Fixer ===\n")

    for module_name, file_path in module_map.items():
        try:
            try:
                text = Path(file_path).read_text(encoding="utf8")
            except UnicodeDecodeError:
                text = Path(file_path).read_text(encoding="latin1")

            tree = ast.parse(text)

        except Exception:
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                import_path = node.module
                if not import_path:
                    continue

                if import_path not in module_map:
                    print("----------------------------------------")
                    print(f"[BROKEN IMPORT] in {module_name}")
                    print(f"  → from {import_path} import ...")

                    suggestion = suggest_fix(import_path, module_map)

                    if suggestion:
                        print(f"[SUGGESTED FIX]")
                        print(f"  → from {suggestion} import ...")
                        choice = input("Apply fix? (y/n): ").strip().lower()
                        if choice == "y":
                            apply_fix(file_path, import_path, suggestion)
                            print("[FIX APPLIED]")
                        else:
                            print("[SKIPPED]")
                    else:
                        print("[NO MATCH FOUND]")

    print("\n=== Import Fixer Completed ===\n")
