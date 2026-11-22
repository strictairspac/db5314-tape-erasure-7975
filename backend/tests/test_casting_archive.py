"""选角批次入档领域的规则测试。

直接在批次根目录运行，无需启动服务：

    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=backend python3 -m unittest discover -s backend/tests -v
"""

import os
import tempfile
import threading
import unittest

from src.services.casting_archive import (
    ARCHIVED, ERASED, PENDING, QUEUED, ArchiveError, open_registry,
)


class RegistryCase(unittest.TestCase):
    def setUp(self):
        self.registry = open_registry(':memory:')

    def tearDown(self):
        self.registry.close()

    def apply(self, *events):
        results = []
        for event in events:
            results.append(self.registry.apply(event))
        return results


class AliasAndContractTests(RegistryCase):
    def test_same_material_uses_different_local_codes(self):
        # 同一份素材在两组选角记录里编号不同，归并到同一规范素材。
        self.apply(
            {'op': 'register_material', 'material_id': 'M-01', 'object_key': 'scene1/take3.mov',
             'aliases': [{'group_code': 'gA', 'local_ref': 'A-9'}]},
            {'op': 'register_material', 'material_id': 'M-01', 'object_key': 'scene1/take3.mov',
             'aliases': [{'group_code': 'gB', 'local_ref': 'B-77'}]},
        )
        self.assertEqual(self.registry._material_by_alias('gA', 'A-9'), 'M-01')
        self.assertEqual(self.registry._material_by_alias('gB', 'B-77'), 'M-01')

    def test_contract_arrives_before_material_then_auto_links(self):
        result = self.registry.register_contract(
            {'contract_id': 'CT-1', 'group_code': 'gA', 'local_ref': 'A-9'})
        self.assertEqual(result['status'], 'pending')
        self.registry.register_material(
            {'material_id': 'M-01', 'object_key': 'a.mov',
             'aliases': [{'group_code': 'gA', 'local_ref': 'A-9'}]})
        row = self.registry.db.execute(
            'SELECT material_id FROM contracts WHERE contract_id = ?', ('CT-1',)).fetchone()
        self.assertEqual(row['material_id'], 'M-01')
        # 挂起队列已清空。
        self.assertEqual(self.registry.db.execute('SELECT COUNT(*) AS n FROM pending_links').fetchone()['n'], 0)

    def test_reference_arrives_before_one_end_then_links(self):
        self.registry.register_reference(
            {'group_code': 'gA', 'referrer_ref': 'A-1', 'dependency_ref': 'A-2'})
        self.registry.register_material(
            {'material_id': 'M-1', 'object_key': 'r.mov',
             'aliases': [{'group_code': 'gA', 'local_ref': 'A-1'}]})
        # 仅引用方到档，关系仍挂起。
        self.assertEqual(self.registry.db.execute('SELECT COUNT(*) AS n FROM material_references').fetchone()['n'], 0)
        self.registry.register_material(
            {'material_id': 'M-2', 'object_key': 'd.mov',
             'aliases': [{'group_code': 'gA', 'local_ref': 'A-2'}]})
        edge = self.registry.db.execute('SELECT * FROM material_references').fetchone()
        self.assertEqual((edge['referrer_material_id'], edge['dependency_material_id']), ('M-1', 'M-2'))

    def test_correction_reassigns_pending_but_keeps_linked_contract_sticky(self):
        # 合同先对上 M-OLD；随后编号改正到 M-NEW，合同仍跟随原素材。
        self.registry.register_material(
            {'material_id': 'M-OLD', 'object_key': 'old.mov',
             'aliases': [{'group_code': 'gA', 'local_ref': 'A-9'}]})
        self.registry.register_contract(
            {'contract_id': 'CT-STICKY', 'group_code': 'gA', 'local_ref': 'A-9'})
        self.registry.register_material(
            {'material_id': 'M-NEW', 'object_key': 'new.mov'})
        changed = self.registry.correct_ref(
            {'group_code': 'gA', 'local_ref': 'A-9', 'correct_material_id': 'M-NEW'})
        self.assertTrue(changed['changed'])
        # 已对上的合同不动。
        linked = self.registry.db.execute(
            'SELECT material_id FROM contracts WHERE contract_id = ?', ('CT-STICKY',)).fetchone()
        self.assertEqual(linked['material_id'], 'M-OLD')
        # 别名已指向新素材。
        self.assertEqual(self.registry._material_by_alias('gA', 'A-9'), 'M-NEW')

    def test_correction_reassigns_still_pending_contract(self):
        # 合同先于素材挂起；编号改正到已到档的正确素材后，挂起合同重新分配。
        self.registry.register_contract(
            {'contract_id': 'CT-PEND', 'group_code': 'gA', 'local_ref': 'A-9'})
        self.registry.register_material(
            {'material_id': 'M-RIGHT', 'object_key': 'right.mov'})
        self.registry.correct_ref(
            {'group_code': 'gA', 'local_ref': 'A-9', 'correct_material_id': 'M-RIGHT'})
        linked = self.registry.db.execute(
            'SELECT material_id FROM contracts WHERE contract_id = ?', ('CT-PEND',)).fetchone()
        self.assertEqual(linked['material_id'], 'M-RIGHT')

    def test_correction_to_unknown_material_rejected(self):
        with self.assertRaises(ArchiveError):
            self.registry.correct_ref(
                {'group_code': 'gA', 'local_ref': 'A-9', 'correct_material_id': 'M-GHOST'})

    def test_duplicate_alias_to_other_material_rejected(self):
        self.registry.register_material(
            {'material_id': 'M-1', 'object_key': 'a.mov',
             'aliases': [{'group_code': 'gA', 'local_ref': 'A-1'}]})
        with self.assertRaises(ArchiveError):
            self.registry.register_material(
                {'material_id': 'M-2', 'object_key': 'b.mov',
                 'aliases': [{'group_code': 'gA', 'local_ref': 'A-1'}]})


class BatchTests(RegistryCase):
    def _material(self, material_id, local_ref, group_code='gA'):
        self.registry.register_material(
            {'material_id': material_id, 'object_key': f'{material_id}.mov',
             'aliases': [{'group_code': group_code, 'local_ref': local_ref}]})

    def test_batch_with_missing_material_is_suspended_others_archive(self):
        self._material('M-1', 'A-1')
        self.registry.submit_batch(
            {'batch_id': 'B-MISS', 'items': [
                {'group_code': 'gA', 'local_ref': 'A-1'}, {'group_code': 'gA', 'local_ref': 'A-2'}]})
        status = self.registry.batch_status('B-MISS')
        self.assertEqual(status['status'], PENDING)
        self.assertEqual(status['missing_materials'], ['gA/A-2'])

        # 另一个素材齐全的批次照常入档。
        self._material('M-3', 'A-3')
        self.registry.submit_batch(
            {'batch_id': 'B-OK', 'items': [{'group_code': 'gA', 'local_ref': 'A-3'}]})
        self.assertEqual(self.registry.batch_status('B-OK')['status'], ARCHIVED)

    def test_missing_material_arrives_then_retry_archives(self):
        self.registry.submit_batch(
            {'batch_id': 'B-1', 'items': [{'group_code': 'gA', 'local_ref': 'A-9'}]})
        self.assertEqual(self.registry.batch_status('B-1')['status'], PENDING)
        self._material('M-9', 'A-9')
        retried = self.registry.retry_batch('B-1')
        self.assertEqual(retried['status'], ARCHIVED)
        self.assertEqual(retried['missing_materials'], [])

    def test_retry_same_batch_is_idempotent(self):
        self._material('M-1', 'A-1')
        first = self.registry.submit_batch(
            {'batch_id': 'B-1', 'items': [{'group_code': 'gA', 'local_ref': 'A-1'}]})
        second = self.registry.submit_batch(
            {'batch_id': 'B-1', 'items': [{'group_code': 'gA', 'local_ref': 'A-1'}]})
        self.assertEqual(first['ingest_count'], 1)
        self.assertEqual(second['ingest_count'], 1)
        self.assertEqual(
            self.registry.db.execute('SELECT COUNT(*) AS n FROM batches').fetchone()['n'], 1)

    def test_failed_ingest_keeps_previous_state(self):
        self._material('M-1', 'A-1')
        self.registry.submit_batch(
            {'batch_id': 'B-1', 'items': [{'group_code': 'gA', 'local_ref': 'A-1'}]})
        before = self.registry.catalog_overview()
        # 重复条目触发校验失败，整个事务回滚。
        with self.assertRaises(ArchiveError):
            self.registry.submit_batch(
                {'batch_id': 'B-BAD',
                 'items': [{'group_code': 'gA', 'local_ref': 'X'},
                           {'group_code': 'gA', 'local_ref': 'X'}]})
        after = self.registry.catalog_overview()
        self.assertEqual(before, after)

    def test_invalid_batch_payload_rejected(self):
        with self.assertRaises(ArchiveError):
            self.registry.submit_batch({'batch_id': 'B', 'items': []})


class ConcurrencyTests(unittest.TestCase):
    def test_concurrent_ingests_keep_each_others_materials(self):
        fd, path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        try:
            errors = []

            def worker(group):
                registry = None
                try:
                    registry = open_registry(path)
                    for n in range(20):
                        material_id = f'{group}-M{n}'
                        registry.register_material(
                            {'material_id': material_id, 'object_key': f'{group}/{n}.mov',
                             'aliases': [{'group_code': group, 'local_ref': f'{group}-L{n}'}]})
                        registry.submit_batch(
                            {'batch_id': f'{group}-B{n}',
                             'items': [{'group_code': group, 'local_ref': f'{group}-L{n}'}]})
                except Exception as exc:  # pragma: no cover - 仅在并发损坏时出现
                    errors.append(exc)
                finally:
                    if registry is not None:
                        registry.close()

            threads = [threading.Thread(target=worker, args=(g,)) for g in ('gA', 'gB', 'gC')]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(errors, [])
            checker = open_registry(path)
            try:
                overview = checker.catalog_overview()
                self.assertEqual(overview['material_count'], 60)
                self.assertEqual(len(overview['archived_batches']), 60)
                self.assertEqual(overview['pending_batches'], [])
            finally:
                checker.close()
        finally:
            for suffix in ('', '-wal', '-shm'):
                if os.path.exists(path + suffix):
                    os.unlink(path + suffix)

    def test_lock_contention_serializes_writers(self):
        # 同一文件库上两个连接并发写，BEGIN IMMEDIATE 使其串行，互不丢数据。
        fd, path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        try:
            first = open_registry(path)
            second = open_registry(path)
            first.db.execute('BEGIN IMMEDIATE')
            first.db.execute("INSERT INTO materials VALUES ('M-1', 'a.mov', 't')")

            def delayed():
                second.register_material({'material_id': 'M-2', 'object_key': 'b.mov'})

            thread = threading.Thread(target=delayed)
            thread.start()
            first.db.execute('COMMIT')
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
            # 新连接读取两个事务分别提交的素材。
            checker = open_registry(path)
            try:
                count = checker.db.execute('SELECT COUNT(*) AS n FROM materials').fetchone()['n']
            finally:
                checker.close()
            self.assertEqual(count, 2)
            first.close()
            second.close()
        finally:
            for suffix in ('', '-wal', '-shm'):
                if os.path.exists(path + suffix):
                    os.unlink(path + suffix)


class ErasureTests(RegistryCase):
    def _seed(self):
        for material_id, local in [('M-1', 'L1'), ('M-2', 'L2'), ('M-3', 'L3')]:
            self.registry.register_material(
                {'material_id': material_id, 'object_key': f'{material_id}.mov',
                 'aliases': [{'group_code': 'gA', 'local_ref': local}]})

    def test_queued_erasure_uses_submitted_catalog_version(self):
        self._seed()
        queued = self.registry.queue_erasure({'material_ids': ['M-1']})
        frozen_version = queued['catalog_version']
        # 排队之后新素材到档并产生新版本，执行仍按旧快照。
        self.registry.register_material({'material_id': 'M-LATE', 'object_key': 'late.mov'})
        report = self.registry.execute_erasure(queued['job_id'])
        self.assertEqual(report['catalog_version'], frozen_version)
        self.assertEqual(report['snapshot_material_count'], 3)
        self.assertNotIn('M-LATE', report['requested'])
        self.assertEqual(report['deleted'], ['M-1'])
        # 主注册表不被隔离擦除影响。
        self.assertIsNotNone(
            self.registry.db.execute(
                'SELECT 1 FROM materials WHERE material_id = ?', ('M-1',)).fetchone())

    def test_erasure_retry_does_not_reexecute(self):
        self._seed()
        queued = self.registry.queue_erasure({'material_ids': ['M-2']})
        first = self.registry.execute_erasure(queued['job_id'])
        second = self.registry.execute_erasure(queued['job_id'])
        self.assertEqual(first, second)
        self.assertEqual(
            self.registry.db.execute('SELECT COUNT(*) AS n FROM erasure_jobs WHERE status = ?',
                                     (ERASED,)).fetchone()['n'], 1)

    def test_contract_protection_blocks_erasure(self):
        self._seed()
        self.registry.register_contract(
            {'contract_id': 'CT-9', 'group_code': 'gA', 'local_ref': 'L1'})
        queued = self.registry.queue_erasure({'material_ids': ['M-1', 'M-2']})
        report = self.registry.execute_erasure(queued['job_id'])
        self.assertEqual(report['blocked_by_protection'], ['M-1'])
        self.assertEqual(report['deleted'], ['M-2'])

    def test_protected_material_keeps_its_dependency_chain(self):
        # M-1 被合同保护且引用 M-3：M-3 作为受保护素材的依赖一并留存。
        self._seed()
        self.registry.register_contract(
            {'contract_id': 'CT-9', 'group_code': 'gA', 'local_ref': 'L1'})
        self.registry.register_reference(
            {'group_code': 'gA', 'referrer_ref': 'L1', 'dependency_ref': 'L3'})
        queued = self.registry.queue_erasure({'material_ids': ['M-1', 'M-2', 'M-3']})
        report = self.registry.execute_erasure(queued['job_id'])
        self.assertEqual(sorted(report['blocked_by_protection']), ['M-1', 'M-3'])
        self.assertEqual(report['deleted'], ['M-2'])

    def test_retained_referrers_keep_shared_dependency(self):
        self._seed()
        # M-1 引用 M-3；仅擦 M-1 时，范围外的 M-3 自然留存。
        self.registry.register_reference(
            {'group_code': 'gA', 'referrer_ref': 'L1', 'dependency_ref': 'L3'})
        queued = self.registry.queue_erasure({'material_ids': ['M-1']})
        report = self.registry.execute_erasure(queued['job_id'])
        self.assertEqual(report['deleted'], ['M-1'])
        self.assertEqual(report['blocked_by_protection'], [])

    def test_shared_dependency_protected_by_outside_referrer(self):
        self._seed()
        # 范围外的 M-2 引用 M-3；M-1 与 M-3 同批擦除时，M-3 被 M-2 的留存引用挡住。
        self.registry.register_reference(
            {'group_code': 'gA', 'referrer_ref': 'L2', 'dependency_ref': 'L3'})
        queued = self.registry.queue_erasure({'material_ids': ['M-1', 'M-3']})
        report = self.registry.execute_erasure(queued['job_id'])
        self.assertEqual(report['deleted'], ['M-1'])
        self.assertEqual(report['blocked_by_protection'], ['M-3'])

    def test_deletion_order_respects_references_between_targets(self):
        # M-1 -> M-2，两者都在擦除范围：引用方 M-1 先删，被引用 M-2 后删。
        self._seed()
        self.registry.register_reference(
            {'group_code': 'gA', 'referrer_ref': 'L1', 'dependency_ref': 'L2'})
        queued = self.registry.queue_erasure({'material_ids': ['M-1', 'M-2']})
        report = self.registry.execute_erasure(queued['job_id'])
        self.assertEqual(report['deleted'], ['M-1', 'M-2'])
        self.assertEqual(report['failures'], [])

    def test_queue_unknown_material_rejected(self):
        self._seed()
        with self.assertRaises(ArchiveError):
            self.registry.queue_erasure({'material_ids': ['M-GHOST']})
        self.assertEqual(
            self.registry.db.execute('SELECT COUNT(*) AS n FROM erasure_jobs').fetchone()['n'], 0)

    def test_queued_job_status(self):
        self._seed()
        queued = self.registry.queue_erasure({'material_ids': ['M-1']})
        row = self.registry.db.execute(
            'SELECT status FROM erasure_jobs WHERE job_id = ?', (queued['job_id'],)).fetchone()
        self.assertEqual(row['status'], QUEUED)


class OverviewTests(RegistryCase):
    def test_overview_reports_each_batch_and_missing_items(self):
        self.registry.register_material(
            {'material_id': 'M-1', 'object_key': 'a.mov',
             'aliases': [{'group_code': 'gA', 'local_ref': 'A-1'}]})
        self.registry.submit_batch(
            {'batch_id': 'B-HOLD', 'items': [
                {'group_code': 'gA', 'local_ref': 'A-1'}, {'group_code': 'gA', 'local_ref': 'A-2'}]})
        self.registry.submit_batch(
            {'batch_id': 'B-DONE', 'items': [{'group_code': 'gA', 'local_ref': 'A-1'}]})
        overview = self.registry.catalog_overview()
        self.assertEqual(overview['pending_batches'], ['B-HOLD'])
        self.assertEqual(overview['archived_batches'], ['B-DONE'])
        held = next(batch for batch in overview['batches'] if batch['batch_id'] == 'B-HOLD')
        self.assertFalse(held['archived'])
        self.assertEqual(held['missing_materials'], ['gA/A-2'])
        missing_item = next(item for item in held['items'] if item['local_ref'] == 'A-2')
        self.assertFalse(missing_item['resolved'])

    def test_validation_messages(self):
        with self.assertRaises(ArchiveError):
            self.registry.register_material({'material_id': '  ', 'object_key': 'a.mov'})
        with self.assertRaises(ArchiveError):
            self.registry.register_contract({'contract_id': 'C', 'group_code': 'g', 'local_ref': ''})
        with self.assertRaises(ArchiveError):
            self.registry.apply({'op': 'unknown_op'})


if __name__ == '__main__':
    unittest.main(verbosity=2)
