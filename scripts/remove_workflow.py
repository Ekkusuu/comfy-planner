"""Take a workflow out of the app, keeping its history. Run it by hand, with the app stopped or idle:

    uv run python scripts/remove_workflow.py <workflow id> --jobs-to <other workflow id>

It deletes the workflow's row, its card and our converted copy under data/workflows/. Its jobs stay (images, prompts,
seeds, recipes) and move to the other workflow; each image's recipe still names the workflow that made it. The file the
workflow was imported from (your ComfyUI user folder) is never touched. Safe to run twice: a workflow that is gone is
reported and nothing else happens. Back up data/comfy-planner.db first."""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_config          # noqa: E402
from app.db import Database                 # noqa: E402
from app.library.workflows import WorkflowLibrary   # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("workflow_id")
    parser.add_argument("--jobs-to", required=True, help="the workflow that takes over the removed workflow's jobs")
    args = parser.parse_args()
    config = load_config()
    db = Database(config.db_path)
    library = WorkflowLibrary(db, config, comfy=None)
    try:
        if library.remove(args.workflow_id, args.jobs_to):
            print(f"Removed {args.workflow_id}; its jobs now belong to {args.jobs_to}.")
        else:
            print(f"{args.workflow_id} is not in the library (already removed): nothing to do.")
    except KeyError as e:
        sys.exit(f"Unknown workflow: {e.args[0]}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
