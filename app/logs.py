"""Log files for troubleshooting, in data/logs/ (next to app.log, which the launcher writes from the server's output).

  planner.log    what the app itself reports: planning steps, LLM attempts and how long they took, the queue worker,
                 card builds, GPU hand-overs, warnings and errors (with tracebacks).
  llm-calls.log  every LLM call in full: the task, the request the LLM got (inputs filled into the prompt file), its raw
                 reply exactly as it came back, and whether the app accepted it. Use it when a plan looks wrong.

Both rotate: when a file reaches its size limit it is renamed to .1 (then .2, ...) and a new one starts, so they never
grow without limit. The LLM calls contain your requests and the cards; nothing in them is sent anywhere.
"""
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

APP_LOGGERS = ("app", "llm", "planning", "worker", "cards", "gpu")
APP_LOG = "planner.log"
LLM_LOG = "llm-calls.log"
MB = 1024 * 1024

llm_calls = logging.getLogger("llm.calls")      # written only to llm-calls.log
llm_calls.propagate = False

_handlers: list[tuple[logging.Logger, logging.Handler]] = []


def setup_logging(logs_dir: Path):
    """Attach the two log files. Calling it again (a new app in the tests) moves them to the new folder."""
    close_logging()
    logs_dir.mkdir(parents=True, exist_ok=True)
    app_file = RotatingFileHandler(logs_dir / APP_LOG, maxBytes=5 * MB, backupCount=3, encoding="utf-8")
    app_file.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)-8s %(message)s", "%Y-%m-%d %H:%M:%S"))
    for name in APP_LOGGERS:
        logger = logging.getLogger(name)
        logger.setLevel(logging.INFO)
        _attach(logger, app_file)
    calls_file = RotatingFileHandler(logs_dir / LLM_LOG, maxBytes=10 * MB, backupCount=5, encoding="utf-8")
    calls_file.setFormatter(logging.Formatter("%(message)s"))
    llm_calls.setLevel(logging.INFO)
    _attach(llm_calls, calls_file)
    # the browser asks for /api/health every few seconds; keep those lines out of app.log
    logging.getLogger("uvicorn.access").addFilter(_quiet_polling)


def close_logging():
    while _handlers:
        logger, handler = _handlers.pop()
        logger.removeHandler(handler)
        handler.close()


def _attach(logger: logging.Logger, handler: logging.Handler):
    logger.addHandler(handler)
    _handlers.append((logger, handler))


def _quiet_polling(record: logging.LogRecord) -> bool:
    return "/api/health" not in record.getMessage()


def log_llm_call(task: str, attempt: int, model: str, seconds: float, finish: str, request: str, reply: str, outcome: str):
    """One block per LLM call: header, the request (the user message; the fixed instructions are in
    config/prompts/<task>.md), the raw reply, and what the app made of it."""
    if not llm_calls.handlers:
        return
    try:
        shown = json.dumps(json.loads(reply), indent=1, ensure_ascii=False)
    except ValueError:
        shown = reply           # not JSON: show it exactly as it came
    llm_calls.info("\n".join([
        f"===== {_now()}  {task}  attempt {attempt}  model {model}  {seconds:.1f} s  finish {finish or '?'}",
        "----- request", request,
        "----- reply (raw)", shown,
        f"----- result: {outcome}", ""]))


def _now() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
