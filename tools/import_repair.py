from logger import get_logger

logger = get_logger(__name__)


def scan_and_fix():
    module_map = find_all_modules()

    logger.info("\n=== ARIA Lite Import Fixer ===\n")

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
                    logger.info("----------------------------------------")
                    logger.warning(f"[BROKEN IMPORT] in {module_name}")
                    logger.warning(f"  → from {import_path} import ...")

                    suggestion = suggest_fix(import_path, module_map)

                    if suggestion:
                        logger.info(f"[SUGGESTED FIX]")
                        logger.info(f"  → from {suggestion} import ...")
                        choice = input("Apply fix? (y/n): ").strip().lower()
                        if choice == "y":
                            apply_fix(file_path, import_path, suggestion)
                            logger.info("[FIX APPLIED]")
                        else:
                            logger.info("[SKIPPED]")
                    else:
                        logger.warning("[NO MATCH FOUND]")

    logger.info("\n=== Import Fixer Completed ===\n")
