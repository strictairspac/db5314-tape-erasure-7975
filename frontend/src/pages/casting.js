import {heading, panel} from '../components/common/heading.js';
import {records} from '../components/common/records.js';
import {esc} from '../utils/dom.js';
import {submit} from '../hooks/submit.js';

const EVENT_TEMPLATES = {
  register_material: {
    label: '素材到档',
    template: {
      material_id: 'M-01', object_key: 'scene/take.mov',
      aliases: [{group_code: 'gA', local_ref: 'A-01'}],
    },
  },
  correct_ref: {
    label: '编号改正',
    template: {group_code: 'gA', local_ref: 'A-01', correct_material_id: 'M-02'},
  },
  register_contract: {
    label: '合同登记（可先于素材）',
    template: {contract_id: 'CT-01', group_code: 'gA', local_ref: 'A-01'},
  },
  register_reference: {
    label: '引用登记（可先于素材）',
    template: {group_code: 'gA', referrer_ref: 'A-01', dependency_ref: 'A-02'},
  },
  submit_batch: {
    label: '提交入档批次',
    template: {batch_id: 'B-01', items: [{group_code: 'gA', local_ref: 'A-01'}]},
  },
  queue_erasure: {
    label: '排队擦除（冻结当前目录版本）',
    template: {material_ids: ['M-01'], note: '排队擦除'},
  },
};

const BATCH_LABELS = {pending: '挂起（缺素材）', archived: '已入档'};

export function castingPage(state) {
  const overview = state.casting;
  const canOperate = state.session.role === 'operator';
  const batchRows = overview.batches.map(batch => [
    esc(batch.batch_id),
    `<span class="status" data-state="${esc(batch.status)}">${esc(BATCH_LABELS[batch.status] || batch.status)}</span>`,
    esc(`${batch.items.length} 项`),
    batch.missing_materials.length
      ? `<strong data-testid="missing">${esc(batch.missing_materials.join('、'))}</strong>`
      : '—',
    esc(batch.archived_at || '—'),
  ]);
  const materialRows = overview.materials.map(material => [esc(material.material_id), esc(material.object_key)]);
  const erasureRows = (state.erasureJobs || []).map(job => [
    esc(job.job_id.slice(0, 8)),
    esc(`v${job.catalog_version}`),
    job.status === 'erased'
      ? '<span class="status" data-state="complete">已执行</span>'
      : '<span class="status" data-state="queued">排队中</span>',
    esc(job.requested_at),
    esc(job.executed_at || '—'),
  ]);
  const selector = Object.entries(EVENT_TEMPLATES)
    .map(([op, item]) => `<option value="${esc(op)}">${esc(item.label)}</option>`).join('');
  const initial = JSON.stringify(EVENT_TEMPLATES.register_material.template, null, 2);
  const eventForm = canOperate ? `
    <form id="casting-event-form">
      <label class="field"><span>领域事件</span>
        <select id="casting-event-op">${selector}</select></label>
      <label class="field data"><span>事件数据（JSON）</span>
        <textarea id="casting-event-data" rows="9" spellcheck="false">${esc(initial)}</textarea></label>
      <div class="actions"><button>提交事件</button></div>
    </form>` : '<p class="note">仅经办员可提交领域事件。</p>';
  const retryButtons = overview.batches
    .filter(batch => batch.status === 'pending')
    .map(batch => `<button type="button" class="secondary casting-retry" data-batch="${esc(batch.batch_id)}">重试 ${esc(batch.batch_id)}</button>`)
    .join('');
  const erasureButtons = (state.erasureJobs || [])
    .filter(job => job.status === 'queued')
    .map(job => `<button type="button" class="secondary casting-execute" data-job="${esc(job.job_id)}">执行 ${esc(job.job_id.slice(0, 8))}</button>`)
    .join('');
  return `${heading('选角批次入档', '组内编号归并、合同先行、缺素材挂起与按版本快照擦除', '')}
    <dl class="metrics">
      <div><dt>目录版本</dt><dd>v${esc(overview.catalog_version)}</dd></div>
      <div><dt>素材数</dt><dd>${esc(overview.material_count)}</dd></div>
      <div><dt>已入档批次</dt><dd>${esc(overview.archived_batches.length)}</dd></div>
      <div><dt>挂起批次</dt><dd>${esc(overview.pending_batches.length)}</dd></div>
    </dl>
    <div class="spread">
      <section>${panel('批次入档状态（挂起批次列出缺失素材）',
        records(['批次', '状态', '条目', '缺失素材', '入档时间'], batchRows, '尚未提交批次。'))}
        ${retryButtons ? `<div class="actions">${retryButtons}</div>` : ''}</section>
      <aside class="brief">${panel('提交领域事件', eventForm)}</aside>
    </div>
    ${panel('规范素材目录', records(['素材编号', '对象路径'], materialRows, '目录为空。'))}
    ${panel('擦除任务（按提交时目录版本执行）',
      records(['任务', '快照版本', '状态', '排队时间', '执行时间'], erasureRows, '暂无擦除任务。'))}
    ${erasureButtons ? `<div class="actions">${erasureButtons}</div>` : ''}`;
}

export function bindCasting(api, refresh, fail) {
  const opSelect = document.getElementById('casting-event-op');
  const dataBox = document.getElementById('casting-event-data');
  opSelect?.addEventListener('change', () => {
    dataBox.value = JSON.stringify(EVENT_TEMPLATES[opSelect.value].template, null, 2);
  });
  submit('casting-event-form', async () => {
    let data;
    try {
      data = JSON.parse(dataBox.value);
    } catch {
      throw new Error('事件数据须为合法 JSON。');
    }
    const result = await api('/casting/events', {method: 'POST', data: {op: opSelect.value, data}});
    if (opSelect.value === 'queue_erasure') {
      // 排队后可直接执行，也可等待 worker；演示页提供立即执行入口。
      await api('/casting/erasure/execute', {method: 'POST', data: {job_id: result.job_id}});
    }
    await refresh();
  }, fail);
  document.querySelectorAll('.casting-retry').forEach(button => {
    button.addEventListener('click', async () => {
      await api(`/casting/batches/${encodeURIComponent(button.dataset.batch)}/retry`, {method: 'POST'});
      await refresh();
    });
  });
  document.querySelectorAll('.casting-execute').forEach(button => {
    button.addEventListener('click', async () => {
      await api('/casting/erasure/execute', {method: 'POST', data: {job_id: button.dataset.job}});
      await refresh();
    });
  });
}
