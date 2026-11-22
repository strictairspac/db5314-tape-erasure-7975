import logging
from pathlib import Path
from threading import Event, Thread
from time import sleep

from src.config.settings import ARTIFACTS, cache, engine
from src.services import casting_registry
from src.services.casting_archive import QUEUED
from src.services.domain import solve
from src.services.workflow import claim, finish

HEARTBEAT = Path(ARTIFACTS) / 'worker-heartbeat'


def heartbeat(stop):
    while not stop.is_set():
        HEARTBEAT.touch()
        stop.wait(2)


def run_due_erasure_jobs():
    """按排队时冻结的目录版本执行擦除；任务级幂等，重试不重复执行。"""
    db = casting_registry.registry().db
    job_ids = [row['job_id'] for row in db.execute(
        'SELECT job_id FROM erasure_jobs WHERE status = ? ORDER BY requested_at', (QUEUED,))]
    for job_id in job_ids:
        try:
            casting_registry.registry().execute_erasure(job_id)
        except Exception:
            logging.exception('Casting erasure job failed: %s', job_id)


def work():
    HEARTBEAT.unlink(missing_ok=True)
    while True:
        try:
            run_due_erasure_jobs()
            job = claim()
            HEARTBEAT.touch()
            if not job:
                cache.brpop('worker:wake', timeout=1)
                continue
            run_id, token, payload = job
            stop = Event()
            thread = Thread(target=heartbeat, args=(stop,), daemon=True)
            thread.start()
            try:
                result = solve(payload, engine)
                finish(run_id, token, result=result)
            except (ValueError, KeyError, TypeError) as exc:
                finish(run_id, token, error=str(exc)[:500])
            except Exception:
                logging.exception('Domain run failed: %s', run_id)
                finish(run_id, token, error='后台执行失败，请检查输入或查看 worker 日志。')
            finally:
                stop.set()
                thread.join()
        except Exception:
            logging.exception('Worker loop failure')
            sleep(2)


if __name__ == '__main__':
    work()
