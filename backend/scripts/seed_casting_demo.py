"""选角批次入档领域的演示数据。

与既有样带演示相互独立，仅写入 artifacts 卷中的领域 SQLite。
重复执行幂等：所有事件都以业务编号去重。
"""

from src.services import casting_registry

DEMO_EVENTS = [
    # 合同先于素材到档。
    {'op': 'register_contract', 'contract_id': 'CT-301', 'group_code': 'gA', 'local_ref': 'A-101'},
    # 同一份素材在两组选角记录中编号不同。
    {'op': 'register_material', 'material_id': 'M-101', 'object_key': 'rainy/take2.mov',
     'aliases': [{'group_code': 'gA', 'local_ref': 'A-101'}, {'group_code': 'gB', 'local_ref': 'B-205'}]},
    {'op': 'register_material', 'material_id': 'M-102', 'object_key': 'rainy/master.mxf',
     'aliases': [{'group_code': 'gA', 'local_ref': 'A-102'}]},
    # 引用先登记，随后两端素材到档后自动对上。
    {'op': 'register_reference', 'group_code': 'gA', 'referrer_ref': 'A-101', 'dependency_ref': 'A-102'},
    # 素材齐全的批次直接入档。
    {'op': 'submit_batch', 'batch_id': 'CB-DONE', 'items': [
        {'group_code': 'gA', 'local_ref': 'A-101'}, {'group_code': 'gA', 'local_ref': 'A-102'}]},
    # 缺素材的批次挂起。
    {'op': 'submit_batch', 'batch_id': 'CB-HOLD', 'items': [
        {'group_code': 'gA', 'local_ref': 'A-101'}, {'group_code': 'gA', 'local_ref': 'A-109'}]},
]


def seed_casting_demo():
    registry = casting_registry.registry()
    for event in DEMO_EVENTS:
        registry.apply(event)
    return registry.catalog_overview()


if __name__ == '__main__':
    print(seed_casting_demo())
