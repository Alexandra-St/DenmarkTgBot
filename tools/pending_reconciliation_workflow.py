"""Fixed entry point sharing the reviewed private-plan maintenance transport."""
from tools.reclassification_workflow import main


if __name__ == "__main__":
    raise SystemExit(main(operation="pending_reconciliation"))
