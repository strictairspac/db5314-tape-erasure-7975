"""选角批次入档与目录擦除的领域服务。

规则来源（业务口述）：

* 同一份素材在各组选角记录里的编号可能不同：别名(alias)归并到规范素材(canonical)。
* 合同有时比素材先到：合同/引用可先挂起到站编号，素材到档后自动对上。
* 编号改正后可以重新分配；已对上素材的合同和引用仍跟着原素材（粘性）。
* 缺素材的批次先挂起，其他批次照常入档。
* 并发入档时要留住别组刚入档的素材；失败后保留旧版；重试同一批不重复入档。
* 已排队的擦除按提交时的目录版本执行（快照）。
* 档案页面能看到每批是否入档，挂起时能看出缺哪项素材。

本模块只依赖标准库，可在独立内存/文件 SQLite 上运行，不接触平台对象存储。
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timezone

# 批次状态
PENDING = 'pending'      # 挂起：存在尚未对上素材的待办项
ARCHIVED = 'archived'    # 已入档：所有条目均已对上素材
# 擦除任务状态
QUEUED = 'queued'
ERASED = 'erased'
# 待办项类型
PENDING_CONTRACT = 'contract'
PENDING_REFERENCE = 'reference'

SCHEMA = '''
CREATE TABLE IF NOT EXISTS materials (
    material_id   TEXT PRIMARY KEY NOT NULL,
    object_key    TEXT NOT NULL UNIQUE,
    created_at    TEXT NOT NULL
);
-- 组内编号 -> 规范素材。同组内编号唯一；不同组可使用同一组内编号。
CREATE TABLE IF NOT EXISTS material_aliases (
    group_code    TEXT NOT NULL,
    local_ref     TEXT NOT NULL,
    material_id   TEXT NOT NULL REFERENCES materials(material_id),
    created_at    TEXT NOT NULL,
    PRIMARY KEY (group_code, local_ref)
);
-- 已对上素材的合同（合同可能先于素材到，因此以合同编号+素材为业务键）。
CREATE TABLE IF NOT EXISTS contracts (
    contract_id   TEXT NOT NULL,
    material_id   TEXT NOT NULL REFERENCES materials(material_id),
    received_at   TEXT NOT NULL,
    PRIMARY KEY (contract_id, material_id)
);
-- 已对上素材的引用：引用方 -> 被引用素材。
CREATE TABLE IF NOT EXISTS material_references (
    referrer_material_id   TEXT NOT NULL REFERENCES materials(material_id),
    dependency_material_id TEXT NOT NULL REFERENCES materials(material_id),
    PRIMARY KEY (referrer_material_id, dependency_material_id)
);
-- 等待素材到档的合同/引用，按组内编号挂起。
-- 合同挂起：local_ref 为待对素材的组内编号；引用挂起：referrer_ref/dependency_ref 显式保存两端编号。
CREATE TABLE IF NOT EXISTS pending_links (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    group_code      TEXT NOT NULL,
    local_ref       TEXT NOT NULL,
    link_type       TEXT NOT NULL CHECK (link_type IN ('contract', 'reference')),
    contract_id     TEXT,
    referrer_ref    TEXT,
    dependency_ref  TEXT,
    created_at      TEXT NOT NULL,
    UNIQUE (group_code, local_ref, link_type, contract_id, referrer_ref, dependency_ref)
);
CREATE TABLE IF NOT EXISTS batches (
    batch_id      TEXT PRIMARY KEY NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('pending', 'archived')),
    submitted_at  TEXT NOT NULL,
    archived_at   TEXT,
    ingest_count  INTEGER NOT NULL DEFAULT 0
);
-- 批次条目：批次内编号唯一，记录要求的组内编号及是否已对上规范素材。
CREATE TABLE IF NOT EXISTS batch_items (
    batch_id      TEXT NOT NULL REFERENCES batches(batch_id),
    local_ref     TEXT NOT NULL,
    group_code    TEXT NOT NULL,
    material_id   TEXT REFERENCES materials(material_id),
    PRIMARY KEY (batch_id, local_ref)
);
CREATE TABLE IF NOT EXISTS catalog_versions (
    version       INTEGER PRIMARY KEY NOT NULL,
    created_at    TEXT NOT NULL,
    note          TEXT
);
-- 每次入档成功后记录目录版本，供状态页面展示。
CREATE TABLE IF NOT EXISTS catalog_snapshot (
    version       INTEGER NOT NULL,
    material_id   TEXT NOT NULL,
    object_key    TEXT NOT NULL,
    PRIMARY KEY (version, material_id)
);
CREATE TABLE IF NOT EXISTS erasure_jobs (
    job_id            TEXT PRIMARY KEY NOT NULL,
    catalog_version   INTEGER NOT NULL,
    status            TEXT NOT NULL CHECK (status IN ('queued', 'erased')),
    requested_at      TEXT NOT NULL,
    executed_at       TEXT,
    report            TEXT
);
CREATE TABLE IF NOT EXISTS erasure_job_targets (
    job_id        TEXT NOT NULL REFERENCES erasure_jobs(job_id),
    material_id   TEXT NOT NULL,
    object_key    TEXT NOT NULL,
    PRIMARY KEY (job_id, material_id)
);
'''


class ArchiveError(ValueError):
    """业务校验失败（输入问题，调用方应修正后重试）。"""


def _now():
    return datetime.now(timezone.utc).isoformat()


def _text(row, key):
    value = row.get(key)
    if not isinstance(value, str) or not value or value != value.strip():
        raise ArchiveError(f'{key} 须为非空文本，且首尾无空格')
    return value


def open_registry(path=':memory:'):
    """打开一个领域注册表。

    内存库适合测试与一次性运行；文件库可被 worker 持久化。
    写事务使用 BEGIN IMMEDIATE，配合锁重试保证并发入档互不覆盖。
    返回 :class:`ArchiveRegistry`，调用方负责在进程退出时 :meth:`close`。
    """
    db = sqlite3.connect(path, timeout=30, check_same_thread=False)
    db.row_factory = sqlite3.Row
    # 关闭隐式事务，全部写操作通过 BEGIN IMMEDIATE 显式管理。
    db.isolation_level = None
    # 文件库启用 WAL：多个连接并发入档时，读者不会阻塞写者，提交对其他连接立即可见。
    if path != ':memory:':
        db.execute('PRAGMA journal_mode = WAL')
    db.execute('PRAGMA foreign_keys = ON')
    db.execute('PRAGMA busy_timeout = 30000')
    db.executescript(SCHEMA)
    db.execute(
        'INSERT INTO catalog_versions (version, created_at, note) '
        "SELECT 0, ?, '初始空目录' WHERE NOT EXISTS (SELECT 1 FROM catalog_versions)",
        (_now(),))
    return ArchiveRegistry(db)


class ArchiveRegistry:
    """对外暴露的领域操作集合。所有写操作都是可重试的显式事务。"""

    def __init__(self, db):
        self.db = db

    def close(self):
        self.db.close()

    # ---------- 内部工具 ----------

    @contextmanager
    def _write(self):
        # BEGIN IMMEDIATE 立即取写锁：并发入档时后到者等待，先到者的提交不会被覆盖。
        attempts = 0
        while True:
            try:
                self.db.execute('BEGIN IMMEDIATE')
                break
            except sqlite3.OperationalError as exc:
                attempts += 1
                if 'database is locked' not in str(exc) or attempts >= 50:
                    raise
                time.sleep(0.05)
        try:
            yield
            self.db.execute('COMMIT')
        except Exception:
            # 任何失败都回滚，旧版数据原样保留。
            self.db.execute('ROLLBACK')
            raise

    def _material_by_alias(self, group_code, local_ref):
        row = self.db.execute(
            'SELECT material_id FROM material_aliases WHERE group_code = ? AND local_ref = ?',
            (group_code, local_ref)).fetchone()
        return row['material_id'] if row else None

    def _current_version(self):
        return self.db.execute('SELECT MAX(version) AS v FROM catalog_versions').fetchone()['v']

    def _new_version(self, note):
        version = self._current_version() + 1
        self.db.execute('INSERT INTO catalog_versions (version, created_at, note) VALUES (?, ?, ?)',
                        (version, _now(), note))
        self.db.executemany(
            'INSERT INTO catalog_snapshot (version, material_id, object_key) SELECT ?, material_id, object_key FROM materials',
            ((version,),))
        return version

    def _resolve_pending(self, group_code, local_ref, material_id):
        """素材到档/改正后，把挂起的合同与引用对上规范素材。"""
        rows = self.db.execute(
            'SELECT * FROM pending_links WHERE group_code = ? AND local_ref = ?',
            (group_code, local_ref)).fetchall()
        for row in rows:
            if row['link_type'] == PENDING_CONTRACT:
                self.db.execute(
                    'INSERT OR IGNORE INTO contracts (contract_id, material_id, received_at) VALUES (?, ?, ?)',
                    (row['contract_id'], material_id, row['created_at']))
                self.db.execute('DELETE FROM pending_links WHERE id = ?', (row['id'],))
                continue
            # 引用挂起行：两端编号显式保存，两端都到齐才对上。
            referrer_material = self._material_by_alias(group_code, row['referrer_ref'])
            dependency_material = self._material_by_alias(group_code, row['dependency_ref'])
            if referrer_material is not None and dependency_material is not None:
                self.db.execute(
                    'INSERT OR IGNORE INTO material_references VALUES (?, ?)',
                    (referrer_material, dependency_material))
                # 同一引用可能在两端各挂一行，两端对上后一并清除。
                self.db.execute(
                    'DELETE FROM pending_links WHERE group_code = ? AND link_type = ? '
                    'AND referrer_ref = ? AND dependency_ref = ?',
                    (group_code, PENDING_REFERENCE, row['referrer_ref'], row['dependency_ref']))

    def _refresh_batch(self, batch_id):
        """根据条目对上情况重算批次状态；挂起批次不会自动入档。"""
        row = self.db.execute('SELECT status FROM batches WHERE batch_id = ?', (batch_id,)).fetchone()
        if row is None or row['status'] == ARCHIVED:
            return
        missing = self.db.execute(
            'SELECT COUNT(*) AS n FROM batch_items WHERE batch_id = ? AND material_id IS NULL',
            (batch_id,)).fetchone()['n']
        if missing == 0:
            self.db.execute(
                'UPDATE batches SET status = ?, archived_at = ? WHERE batch_id = ? AND status = ?',
                (ARCHIVED, _now(), batch_id, PENDING))

    # ---------- 素材与别名 ----------

    def register_material(self, payload):
        """素材到档：登记规范素材与组内别名，并尝试归并挂起项。"""
        material_id = _text(payload, 'material_id')
        object_key = _text(payload, 'object_key')
        aliases = payload.get('aliases', [])
        if not isinstance(aliases, list):
            raise ArchiveError('aliases 须为数组')
        normalized = []
        seen = set()
        for alias in aliases:
            if not isinstance(alias, dict):
                raise ArchiveError('aliases 每行须包含 group_code 与 local_ref')
            group_code = _text(alias, 'group_code')
            local_ref = _text(alias, 'local_ref')
            if (group_code, local_ref) in seen:
                raise ArchiveError('同一批次的别名重复')
            seen.add((group_code, local_ref))
            normalized.append((group_code, local_ref))

        with self._write():
            existing = self.db.execute('SELECT material_id FROM materials WHERE material_id = ? OR object_key = ?',
                                       (material_id, object_key)).fetchone()
            created = False
            if existing is None:
                self.db.execute('INSERT INTO materials VALUES (?, ?, ?)', (material_id, object_key, _now()))
                created = True
            else:
                # 同一对象保持单一规范节点；重复登记视为幂等的别名补充。
                material_id = existing['material_id']
            for group_code, local_ref in normalized:
                owner = self.db.execute(
                    'SELECT material_id FROM material_aliases WHERE group_code = ? AND local_ref = ?',
                    (group_code, local_ref)).fetchone()
                if owner is not None:
                    if owner['material_id'] != material_id:
                        raise ArchiveError(
                            f'组内编号 {group_code}/{local_ref} 已归并到其他素材，须先执行编号改正')
                    continue
                self.db.execute('INSERT INTO material_aliases VALUES (?, ?, ?, ?)',
                                (group_code, local_ref, material_id, _now()))
                self._resolve_pending(group_code, local_ref, material_id)
                # 归并到该别名的批次条目随之对上。
                self.db.execute(
                    'UPDATE batch_items SET material_id = ? WHERE group_code = ? AND local_ref = ? AND material_id IS NULL',
                    (material_id, group_code, local_ref))
            # 直接以规范素材编号登记的批次条目也对上。
            self.db.execute('UPDATE batch_items SET material_id = ? WHERE local_ref = ? AND material_id IS NULL',
                            (material_id, material_id))
            for batch_row in self.db.execute('SELECT DISTINCT batch_id FROM batch_items WHERE material_id = ?',
                                             (material_id,)).fetchall():
                self._refresh_batch(batch_row['batch_id'])
            if created:
                self._new_version(f'素材到档 {material_id}')
            return {'material_id': material_id, 'created': created}

    def correct_ref(self, payload):
        """编号改正：把组内编号重新分配给正确的规范素材。

        仅重新分配仍挂起（未对上素材）的条目；已对上素材的合同和引用保持原素材不动。
        """
        group_code = _text(payload, 'group_code')
        local_ref = _text(payload, 'local_ref')
        correct_material_id = _text(payload, 'correct_material_id')
        with self._write():
            target = self.db.execute('SELECT 1 FROM materials WHERE material_id = ?',
                                     (correct_material_id,)).fetchone()
            if target is None:
                raise ArchiveError('目标素材尚未到档，无法重新分配')
            owner = self.db.execute(
                'SELECT material_id FROM material_aliases WHERE group_code = ? AND local_ref = ?',
                (group_code, local_ref)).fetchone()
            if owner is not None and owner['material_id'] == correct_material_id:
                return {'group_code': group_code, 'local_ref': local_ref,
                        'material_id': correct_material_id, 'changed': False}
            self.db.execute(
                'INSERT INTO material_aliases (group_code, local_ref, material_id, created_at) VALUES (?, ?, ?, ?) '
                'ON CONFLICT(group_code, local_ref) DO UPDATE SET material_id = excluded.material_id',
                (group_code, local_ref, correct_material_id, _now()))
            # 挂起项（合同/引用/批次条目）重新分配；已对上的合同与引用不动。
            self._resolve_pending(group_code, local_ref, correct_material_id)
            self.db.execute(
                'UPDATE batch_items SET material_id = ? WHERE group_code = ? AND local_ref = ? AND material_id IS NULL',
                (correct_material_id, group_code, local_ref))
            for batch_row in self.db.execute(
                    'SELECT DISTINCT batch_id FROM batch_items WHERE group_code = ? AND local_ref = ?',
                    (group_code, local_ref)).fetchall():
                self._refresh_batch(batch_row['batch_id'])
            self._new_version(f'编号改正 {group_code}/{local_ref} -> {correct_material_id}')
            return {'group_code': group_code, 'local_ref': local_ref,
                    'material_id': correct_material_id, 'changed': True}

    # ---------- 合同与引用（可先于素材到） ----------

    def register_contract(self, payload):
        """登记合同留存关联。素材未到档时挂起，到档后自动对上。"""
        contract_id = _text(payload, 'contract_id')
        group_code = _text(payload, 'group_code')
        local_ref = _text(payload, 'local_ref')
        with self._write():
            material_id = self._material_by_alias(group_code, local_ref)
            if material_id is None:
                self.db.execute(
                    'INSERT OR IGNORE INTO pending_links (group_code, local_ref, link_type, contract_id, created_at) '
                    "VALUES (?, ?, 'contract', ?, ?)",
                    (group_code, local_ref, contract_id, _now()))
                return {'contract_id': contract_id, 'status': 'pending', 'material_id': None}
            self.db.execute('INSERT OR IGNORE INTO contracts (contract_id, material_id, received_at) VALUES (?, ?, ?)',
                            (contract_id, material_id, _now()))
            return {'contract_id': contract_id, 'status': 'linked', 'material_id': material_id}

    def register_reference(self, payload):
        """登记素材引用。任一端未到档时挂起，双方到档后自动对上。"""
        group_code = _text(payload, 'group_code')
        referrer_ref = _text(payload, 'referrer_ref')
        dependency_ref = _text(payload, 'dependency_ref')
        with self._write():
            referrer = self._material_by_alias(group_code, referrer_ref)
            dependency = self._material_by_alias(group_code, dependency_ref)
            if referrer is not None and dependency is not None:
                self.db.execute('INSERT OR IGNORE INTO material_references VALUES (?, ?)',
                                (referrer, dependency))
                return {'status': 'linked', 'referrer_material_id': referrer,
                        'dependency_material_id': dependency}
            # 两端都写挂起行：无论哪一端素材先到档，都能触发解析；
            # 两端到齐后 _resolve_pending 会一并清除这两行。
            self.db.execute(
                'INSERT OR IGNORE INTO pending_links '
                '(group_code, local_ref, link_type, referrer_ref, dependency_ref, created_at) '
                "VALUES (?, ?, 'reference', ?, ?, ?)",
                (group_code, referrer_ref, referrer_ref, dependency_ref, _now()))
            self.db.execute(
                'INSERT OR IGNORE INTO pending_links '
                '(group_code, local_ref, link_type, referrer_ref, dependency_ref, created_at) '
                "VALUES (?, ?, 'reference', ?, ?, ?)",
                (group_code, dependency_ref, referrer_ref, dependency_ref, _now()))
            return {'status': 'pending', 'referrer_material_id': referrer,
                    'dependency_material_id': dependency}

    # ---------- 批次与入档 ----------

    def submit_batch(self, payload):
        """提交一个入档批次并尝试入档。

        重复提交同一 batch_id 直接返回当前状态，不产生第二条入档记录。
        缺素材的批次挂起；同事务内其他已就绪批次不受影响。
        """
        batch_id = _text(payload, 'batch_id')
        items = payload.get('items')
        if not isinstance(items, list) or not items:
            raise ArchiveError('批次至少包含一个条目')
        normalized = []
        seen = set()
        for item in items:
            if not isinstance(item, dict):
                raise ArchiveError('批次条目须为对象')
            group_code = _text(item, 'group_code')
            local_ref = _text(item, 'local_ref')
            if local_ref in seen:
                raise ArchiveError(f'批次 {batch_id} 内条目 {local_ref} 重复')
            seen.add(local_ref)
            normalized.append((group_code, local_ref))

        with self._write():
            existing = self.db.execute('SELECT status FROM batches WHERE batch_id = ?', (batch_id,)).fetchone()
            if existing is not None:
                # 幂等：重试同一批不重复入档。
                return self.batch_status(batch_id)
            self.db.execute('INSERT INTO batches (batch_id, status, submitted_at) VALUES (?, ?, ?)',
                            (batch_id, PENDING, _now()))
            for group_code, local_ref in normalized:
                material_id = self._material_by_alias(group_code, local_ref)
                if material_id is None and self.db.execute(
                        'SELECT 1 FROM materials WHERE material_id = ?', (local_ref,)).fetchone():
                    material_id = local_ref
                self.db.execute(
                    'INSERT INTO batch_items (batch_id, local_ref, group_code, material_id) VALUES (?, ?, ?, ?)',
                    (batch_id, local_ref, group_code, material_id))
            return self._ingest_locked(batch_id)

    def _ingest_locked(self, batch_id):
        """调用方须已持有写事务。"""
        batch = self.db.execute('SELECT * FROM batches WHERE batch_id = ?', (batch_id,)).fetchone()
        if batch is None:
            raise ArchiveError('批次不存在')
        if batch['status'] == ARCHIVED:
            return self.batch_status(batch_id)
        missing_rows = self.db.execute(
            'SELECT group_code, local_ref FROM batch_items WHERE batch_id = ? AND material_id IS NULL ORDER BY local_ref',
            (batch_id,)).fetchall()
        if missing_rows:
            return self.batch_status(batch_id)
        # 全部条目已对上素材：入档并产生新的目录版本。
        self.db.execute(
            'UPDATE batches SET status = ?, archived_at = ?, ingest_count = ingest_count + 1 WHERE batch_id = ?',
            (ARCHIVED, _now(), batch_id))
        self._new_version(f'批次入档 {batch_id}')
        return self.batch_status(batch_id)

    def retry_batch(self, batch_id):
        """素材补齐后重试挂起批次；已入档批次保持幂等。"""
        with self._write():
            return self._ingest_locked(_text({'batch_id': batch_id}, 'batch_id'))

    # ---------- 查询：档案页面 ----------

    def batch_status(self, batch_id):
        batch = self.db.execute('SELECT * FROM batches WHERE batch_id = ?', (batch_id,)).fetchone()
        if batch is None:
            raise ArchiveError('批次不存在')
        items = [
            {'local_ref': row['local_ref'], 'group_code': row['group_code'],
             'material_id': row['material_id'],
             'resolved': row['material_id'] is not None}
            for row in self.db.execute(
                'SELECT * FROM batch_items WHERE batch_id = ? ORDER BY local_ref', (batch_id,)).fetchall()
        ]
        missing = [f"{row['group_code']}/{row['local_ref']}" for row in items if not row['resolved']]
        return {
            'batch_id': batch_id,
            'status': batch['status'],
            'archived': batch['status'] == ARCHIVED,
            'submitted_at': batch['submitted_at'],
            'archived_at': batch['archived_at'],
            'ingest_count': batch['ingest_count'],
            'items': items,
            'missing_materials': missing,
        }

    def list_batches(self):
        return [self.batch_status(row['batch_id'])
                for row in self.db.execute('SELECT batch_id FROM batches ORDER BY submitted_at, batch_id')]

    def catalog_overview(self):
        """档案页面总览：目录版本、素材、批次入档情况与挂起缺口。"""
        version = self._current_version()
        materials = [dict(row) for row in self.db.execute(
            'SELECT material_id, object_key FROM materials ORDER BY material_id')]
        batches = self.list_batches()
        return {
            'catalog_version': version,
            'material_count': len(materials),
            'materials': materials,
            'batches': batches,
            'pending_batches': [b['batch_id'] for b in batches if not b['archived']],
            'archived_batches': [b['batch_id'] for b in batches if b['archived']],
        }

    # ---------- 擦除：提交时快照、按快照执行 ----------

    def queue_erasure(self, payload):
        """排队擦除。冻结提交时的目录版本与目标素材快照。"""
        targets = payload.get('material_ids')
        if not isinstance(targets, list) or not targets:
            raise ArchiveError('擦除目标 material_ids 须为非空数组')
        targets = [_text({'material_id': value}, 'material_id') for value in targets]
        note = payload.get('note', '排队擦除')
        with self._write():
            version = self._current_version()
            job_id = uuid.uuid4().hex
            self.db.execute('INSERT INTO erasure_jobs (job_id, catalog_version, status, requested_at, report) '
                            'VALUES (?, ?, ?, ?, NULL)', (job_id, version, QUEUED, _now()))
            for material_id in dict.fromkeys(targets):
                row = self.db.execute('SELECT object_key FROM materials WHERE material_id = ?',
                                      (material_id,)).fetchone()
                if row is None:
                    raise ArchiveError(f'素材 {material_id} 不在当前目录中，无法排队擦除')
                self.db.execute('INSERT INTO erasure_job_targets VALUES (?, ?, ?)',
                                (job_id, material_id, row['object_key']))
            self._new_version(f'{note} {job_id}')
            return {'job_id': job_id, 'catalog_version': version, 'status': QUEUED,
                    'targets': list(dict.fromkeys(targets))}

    def execute_erasure(self, job_id):
        """在隔离内存副本中按排队时的目录版本执行擦除，返回执行证据。"""
        job = self.db.execute('SELECT * FROM erasure_jobs WHERE job_id = ?', (job_id,)).fetchone()
        if job is None:
            raise ArchiveError('擦除任务不存在')
        if job['status'] == ERASED:
            return json.loads(job['report'])
        version = job['catalog_version']
        snapshot = self.db.execute(
            'SELECT material_id, object_key FROM catalog_snapshot WHERE version = ? ORDER BY material_id',
            (version,)).fetchall()
        targets = [dict(row) for row in self.db.execute(
            'SELECT material_id, object_key FROM erasure_job_targets WHERE job_id = ? ORDER BY material_id',
            (job_id,))]

        target_ids = {row['material_id'] for row in targets}
        snapshot_ids = {row['material_id'] for row in snapshot}
        unknown = sorted(target_ids - snapshot_ids)
        if unknown:
            raise ArchiveError(f'擦除目标在提交时目录版本 {version} 中不存在：{unknown}')

        # 读取提交时版本下的合同与引用关系（主库只做快照读取）。
        placeholders = ','.join('?' * len(snapshot_ids))
        hold_rows = self.db.execute(
            f'SELECT contract_id, material_id FROM contracts WHERE material_id IN ({placeholders})',
            tuple(snapshot_ids)).fetchall()
        ref_rows = self.db.execute(
            f'SELECT referrer_material_id, dependency_material_id FROM material_references '
            f'WHERE referrer_material_id IN ({placeholders}) AND dependency_material_id IN ({placeholders})',
            tuple(snapshot_ids) + tuple(snapshot_ids)).fetchall()

        # 隔离副本：擦除只在内存中发生，生产素材与主注册表不受影响。
        with closing(sqlite3.connect(':memory:')) as scratch:
            scratch.isolation_level = None
            scratch.row_factory = sqlite3.Row
            scratch.execute('PRAGMA foreign_keys = ON')
            scratch.executescript('''
                CREATE TABLE scratch_materials (
                    material_id TEXT PRIMARY KEY NOT NULL,
                    object_key TEXT NOT NULL
                );
                CREATE TABLE scratch_holds (
                    contract_id TEXT NOT NULL,
                    material_id TEXT NOT NULL REFERENCES scratch_materials(material_id) ON DELETE RESTRICT
                );
                CREATE TABLE scratch_refs (
                    referrer_material_id TEXT NOT NULL REFERENCES scratch_materials(material_id) ON DELETE RESTRICT,
                    dependency_material_id TEXT NOT NULL REFERENCES scratch_materials(material_id) ON DELETE RESTRICT
                );
            ''')
            scratch.executemany('INSERT INTO scratch_materials VALUES (?, ?)',
                                [(row['material_id'], row['object_key']) for row in snapshot])
            scratch.executemany('INSERT INTO scratch_holds VALUES (?, ?)',
                                [(row['contract_id'], row['material_id']) for row in hold_rows])
            scratch.executemany('INSERT INTO scratch_refs VALUES (?, ?)',
                                [(row['referrer_material_id'], row['dependency_material_id']) for row in ref_rows])

            # 受合同保护、或被范围外留存素材（直接或间接）引用的目标不得擦除。
            protected = {row['material_id'] for row in scratch.execute(
                'SELECT DISTINCT material_id FROM scratch_holds')}
            retained_referrers = snapshot_ids - target_ids
            # 留存引用方自身也在保护范围内（例如被合同挡住的目标）。
            protected |= retained_referrers
            # 沿引用边闭包扩散：留存素材依赖的整条依赖链都继续留存。
            frontier = set(protected)
            while frontier:
                current = frontier
                placeholders = ','.join('?' * len(current))
                dependencies = {row['dependency_material_id'] for row in scratch.execute(
                    f'SELECT DISTINCT dependency_material_id FROM scratch_refs '
                    f'WHERE referrer_material_id IN ({placeholders})', tuple(current))}
                frontier = dependencies - protected
                protected |= frontier
            deletable = target_ids - protected
            blocked = sorted(target_ids & protected)

            deleted, failures = [], []
            scratch.execute('BEGIN')
            try:
                # 多轮排程：每轮删除在剩余目标中已无入边（无其他待删素材引用它）的节点，
                # 同轮按编号排序；RESTRICT 外键阻止任何乱序删除。
                pending = set(deletable)
                while pending:
                    incoming = {row['dependency_material_id'] for row in scratch.execute(
                        'SELECT dependency_material_id FROM scratch_refs WHERE dependency_material_id IN (%s)'
                        % ','.join('?' * len(pending)), tuple(pending))}
                    ready = sorted(pending - incoming)
                    if not ready:
                        failures.append({'material_id': sorted(pending), 'reason': '目标素材之间存在环形引用'})
                        break
                    for material_id in ready:
                        # 先解除该素材作为引用方的出边，再删除；RESTRICT 阻止乱序。
                        scratch.execute('DELETE FROM scratch_refs WHERE referrer_material_id = ?',
                                        (material_id,))
                        scratch.execute('DELETE FROM scratch_materials WHERE material_id = ?', (material_id,))
                        deleted.append(material_id)
                        pending.remove(material_id)
                if failures or scratch.execute('PRAGMA foreign_key_check').fetchone() is not None:
                    raise ArchiveError('隔离副本外键校验失败，擦除已整体回滚')
                scratch.execute('COMMIT')
            except Exception:
                scratch.execute('ROLLBACK')
                raise

        report = {
            'job_id': job_id,
            'catalog_version': version,
            'executed_at': _now(),
            'snapshot_material_count': len(snapshot),
            'requested': sorted(target_ids),
            'deleted': deleted,
            'blocked_by_protection': blocked,
            'failures': failures,
        }
        with self._write():
            # 双重检查，保证同一任务重试不重复执行。
            current = self.db.execute('SELECT status FROM erasure_jobs WHERE job_id = ?', (job_id,)).fetchone()
            if current['status'] == ERASED:
                return json.loads(self.db.execute(
                    'SELECT report FROM erasure_jobs WHERE job_id = ?', (job_id,)).fetchone()['report'])
            self.db.execute(
                'UPDATE erasure_jobs SET status = ?, executed_at = ?, report = ? WHERE job_id = ?',
                (ERASED, report['executed_at'], json.dumps(report, ensure_ascii=False), job_id))
        return report

    # ---------- 事件重放：便于 API/测试按操作清单驱动 ----------

    def apply(self, event):
        kind = event.get('op')
        body = {key: value for key, value in event.items() if key != 'op'}
        handlers = {
            'register_material': self.register_material,
            'correct_ref': self.correct_ref,
            'register_contract': self.register_contract,
            'register_reference': self.register_reference,
            'submit_batch': self.submit_batch,
            'retry_batch': self.retry_batch,
            'queue_erasure': self.queue_erasure,
        }
        if kind == 'execute_erasure':
            return self.execute_erasure(body['job_id'])
        if kind not in handlers:
            raise ArchiveError(f'未知操作：{kind}')
        return handlers[kind](body)
