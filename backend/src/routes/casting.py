"""选角批次入档领域的 HTTP 路由。"""

from fastapi import APIRouter, Depends, HTTPException

from src.schemas.workflow import CastingEventForm, ErasureExecuteForm
from src.services import casting_registry
from src.services.casting_archive import ArchiveError
from src.services.security import current_user, operator

router = APIRouter(prefix='/casting', tags=['casting'])


def _handle(call):
    try:
        return call()
    except ArchiveError as exc:
        raise HTTPException(422, str(exc)) from None


@router.get('/overview')
def overview(_=Depends(current_user)):
    """档案页面：目录版本、每批入档状态与挂起缺口。"""
    return casting_registry.registry().catalog_overview()


@router.get('/batches/{batch_id}')
def batch_status(batch_id: str, _=Depends(current_user)):
    return _handle(lambda: casting_registry.registry().batch_status(batch_id))


@router.get('/erasure')
def list_erasure_jobs(_=Depends(current_user)):
    db = casting_registry.registry().db
    return [dict(row) for row in db.execute(
        'SELECT job_id, catalog_version, status, requested_at, executed_at '
        'FROM erasure_jobs ORDER BY requested_at DESC LIMIT 100')]


@router.post('/events', status_code=201)
def emit_event(form: CastingEventForm, actor=Depends(operator)):
    """经办员提交领域事件（素材到档、合同、引用、批次、擦除排队）。"""
    event = {'op': form.op, **form.data}
    return _handle(lambda: casting_registry.registry().apply(event))


@router.post('/erasure/execute', status_code=201)
def execute_erasure(form: ErasureExecuteForm, actor=Depends(operator)):
    """执行已排队的擦除任务；按排队时冻结的目录版本运行。"""
    return _handle(lambda: casting_registry.registry().execute_erasure(form.job_id))


@router.post('/batches/{batch_id}/retry', status_code=201)
def retry_batch(batch_id: str, actor=Depends(operator)):
    return _handle(lambda: casting_registry.registry().retry_batch(batch_id))
