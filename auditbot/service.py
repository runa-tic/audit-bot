"""The long-running process: scheduler (collect + deliver), bot poller, intake worker, API.

  python -m auditbot.service
"""
import logging
import signal
import threading

from . import api, bot, brand, config, db, jobs, people


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    log = logging.getLogger("audit")
    db.conn()
    b = brand.load()  # a broken brand file stops the start here, not at the first card
    log.info("brand: %s (%s)", b["name"], config.BRAND_PATH or "default theme")
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    threads = []
    if config.BOT_TOKEN:
        threads += [threading.Thread(target=bot.poll, args=(stop,), name="bot", daemon=True),
                    threading.Thread(target=bot.intake_worker, args=(stop,), name="intake", daemon=True)]
    else:
        log.warning("AUDIT_BOT_TOKEN unset: bot off")
    if config.API_TOKEN:
        threads.append(threading.Thread(target=api.serve, args=(stop,), name="api", daemon=True))
    else:
        log.warning("AUDIT_API_TOKEN unset: API off")
    for t in threads:
        t.start()
    log.info("up: cadence %d min, window %d h, venues %s, roots %d",
             config.CADENCE_MIN, config.WINDOW_H, ",".join(config.VENUES), len(people.roots()))
    jobs.scheduler(stop)


if __name__ == "__main__":
    main()
