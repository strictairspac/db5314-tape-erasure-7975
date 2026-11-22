"""选角批次入档领域与现有工作流的适配层。

领域状态保存在 artifacts 卷的 SQLite 文件中，worker 与 API 进程通过
BEGIN IMMEDIATE 串行化写事务；内存库仅用于单元测试。
"""

import os
from functools import lru_cache

from src.config.settings import ARTIFACTS
from src.services.casting_archive import ArchiveError, open_registry

REGISTRY_PATH = os.getenv(
    'CASTING_REGISTRY_DB', os.path.join(ARTIFACTS, 'casting-registry.db'))


@lru_cache(maxsize=1)
def registry():
    os.makedirs(os.path.dirname(REGISTRY_PATH), exist_ok=True)
    # 进程内长期持有的单例；连接随进程存活，写事务由 BEGIN IMMEDIATE 串行化。
    return open_registry(REGISTRY_PATH)


# 领域事件的中文说明，供表单与页面展示。
EVENT_LABELS = {
    'register_material': '素材到档',
    'correct_ref': '编号改正',
    'register_contract': '合同登记',
    'register_reference': '引用登记',
    'submit_batch': '提交批次',
    'retry_batch': '重试批次',
    'queue_erasure': '排队擦除',
    'execute_erasure': '执行擦除',
}

BATCH_STATUS_LABELS = {'pending': '挂起（缺素材）', 'archived': '已入档'}
ERASURE_STATUS_LABELS = {'queued': '排队中（按提交时版本）', 'erased': '已执行'}

__all__ = ['registry', 'ArchiveError', 'EVENT_LABELS', 'BATCH_STATUS_LABELS',
           'ERASURE_STATUS_LABELS']
