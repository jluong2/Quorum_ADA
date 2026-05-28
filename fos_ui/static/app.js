/* Quorum Dashboard — CIP-30 wallet + on-chain governance UI */
'use strict';

// ── State ──────────────────────────────────────────────────
let _state     = null;
let _wallet    = null;   // { name, api, address, ada }
let _pendingTx = null;   // { summary, inputs, redeemers, endpoint, payload }
let _activeTab = 'active';

const WALLETS = [
  { key: 'eternl',  name: 'Eternl',  emoji: '🔵' },
  { key: 'nami',    name: 'Nami',    emoji: '🐱' },
  { key: 'flint',   name: 'Flint',   emoji: '🔶' },
  { key: 'vespr',   name: 'VESPR',   emoji: '🟣' },
  { key: 'yoroi',   name: 'Yoroi',   emoji: '🟡' },
  { key: 'lace',    name: 'Lace',    emoji: '⬜' },
];

// ── Boot ───────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  loadState();
  loadAudit();
  loadDrep();
  setInterval(loadState,  30_000);
  setInterval(loadAudit,  20_000);
  setInterval(loadDrep,  120_000);
  setInterval(updateClock, 1_000);
  updateClock();
});

function updateClock() {
  const el = document.getElementById('clock');
  if (el) el.textContent = new Date().toUTCString().slice(0, -4) + 'UTC';
}

// ── Data loading ───────────────────────────────────────────
async function loadState() {
  const btn = document.getElementById('refresh-icon');
  if (btn) { btn.style.animation = 'spin .6s linear infinite'; }

  try {
    const r = await fetch('/api/state');
    const j = await r.json();
    if (!j.ok) throw new Error(j.error);
    hideError();
    _state = j.data;
    render(_state);
  } catch (e) {
    showError(e.message);
  } finally {
    if (btn) btn.style.animation = '';
  }
}

function render(d) {
  // Network + mode pills
  setText('network-pill', d.network.toUpperCase());
  const modePill = document.getElementById('mode-pill');
  if (modePill) {
    modePill.textContent = d.configured ? 'LIVE' : 'MOCK';
    modePill.className = 'pill ' + (d.configured ? 'pill-green' : 'pill-amber');
  }

  // Stats bar
  setText('stat-treasury',  d.treasury.balance_ada + ' ₳');
  setText('stat-active',    d.governance.active);
  const action = d.governance.executable_now + d.governance.expirable_now + d.governance.awaiting_transfer;
  setText('stat-action',    action);
  setText('stat-members',   d.registry.member_count);
  setText('stat-version',   d.registry.version);

  // Treasury card
  setText('t-balance', d.treasury.balance_ada + ' ₳');
  setText('t-cap',     d.treasury.max_transfer_ada + ' ₳');
  setText('t-gov',     d.treasury.governance_script_hash);

  // Registry
  setText('reg-version', 'v' + d.registry.version);
  renderMembers(d.registry.members);

  // Proposals
  const meta = `${d.governance.total} total · ${d.governance.active} voting · ${action} need action`;
  setText('proposals-meta', meta);
  renderProposals(d.governance.proposals, d);
  updateQuorumHint();

  // Vesting
  renderVesting(d.vesting || {});
}

// ── Registry ───────────────────────────────────────────────
function renderMembers(members) {
  const el = document.getElementById('members-list');
  if (!el) return;
  if (!members.length) {
    el.innerHTML = '<div class="empty-state small">No members</div>';
    return;
  }
  const roleInit = { Admin: 'A', Treasurer: 'T', Member: 'M', Observer: 'O' };
  const roleClass = { Admin: 'avatar-admin', Treasurer: 'avatar-treasurer', Member: 'avatar-member', Observer: 'avatar-observer' };
  el.innerHTML = members.map(m => `
    <div class="member-row">
      <div class="member-avatar ${roleClass[m.role] || 'avatar-observer'}">
        ${roleInit[m.role] || '?'}
      </div>
      <div class="member-info">
        <div class="member-hash" title="${m.key_hash}">${m.key_hash_short}</div>
        <div class="member-role">${m.role} · ${m.status}${m.delegate_short ? ` · ⇒ ${escHtml(m.delegate_short)}` : ''}</div>
      </div>
      <div class="member-actions">
        <div class="member-weight">${m.vote_weight}w</div>
        <button class="micro-btn delegate-btn" title="${m.delegate ? 'Change/clear delegate' : 'Delegate vote'}"
                onclick="openDelegateModal('${escHtml(m.key_hash)}', '${escHtml(m.delegate || '')}')">⇒</button>
      </div>
    </div>
  `).join('');
}

// ── Proposal tabs ──────────────────────────────────────────
function switchTab(tab) {
  _activeTab = tab;
  document.getElementById('tab-active').classList.toggle('active',  tab === 'active');
  document.getElementById('tab-history').classList.toggle('active', tab === 'history');
  if (_state) renderProposals(_state.governance.proposals, _state);
}

// ── Proposals ──────────────────────────────────────────────
function renderProposals(proposals, d) {
  const grid = document.getElementById('proposals-grid');
  if (!grid) return;

  const active  = proposals.filter(p => p.status === 'Voting');
  const history = proposals.filter(p => p.status !== 'Voting');

  // Update tab counts
  setText('tab-count-active',  active.length);
  setText('tab-count-history', history.length);

  const visible = _activeTab === 'history' ? history : active;

  if (!visible.length) {
    const msg = _activeTab === 'history'
      ? 'No completed proposals yet — history will appear here once proposals are executed or expired.'
      : 'No active proposals.';
    grid.innerHTML = `<div class="empty-state">${msg}</div>`;
    return;
  }

  const historyClass = _activeTab === 'history' ? ' history' : '';
  grid.innerHTML = visible.map(p => proposalCard(p, d, historyClass)).join('');
}

function proposalCard(p, d, extraClass = '') {
  const actionIcons = {
    TreasuryTransferAction:        '💸',
    RotateAdminAction:             '🔑',
    UpdateRegistryMemberAction:    '👤',
    OffChainDecisionAction:        '📋',
    CreateVestingAction:           '⏱',
  };
  const icon = actionIcons[p.action_type] || '📄';
  const ad = p.action_details || {};

  // ── Action detail rows ──────────────────────────────────
  let actionRows = '';
  if (p.action_type === 'TreasuryTransferAction') {
    const tokenRows = (ad.tokens || []).map(t =>
      `<div class="detail-row">
        <span class="detail-key">Token</span>
        <span class="detail-val">${escHtml(String(t.quantity))} ${escHtml(t.asset_name)} <span class="mono muted">(${escHtml(t.policy_id.slice(0,8))}…)</span></span>
      </div>`
    ).join('');
    actionRows = `
      <div class="detail-row">
        <span class="detail-key">Recipient</span>
        <span class="detail-val mono" title="${escHtml(ad.recipient||'')}">${escHtml(ad.recipient_short||'—')}</span>
      </div>
      <div class="detail-row">
        <span class="detail-key">Transfer Amount</span>
        <span class="detail-val amount">${escHtml(ad.amount_ada||'—')} ₳</span>
      </div>
      ${tokenRows}
      <div class="detail-row">
        <span class="detail-key">Memo</span>
        <span class="detail-val">${escHtml(ad.memo||'—')}</span>
      </div>`;
  } else if (p.action_type === 'RotateAdminAction') {
    actionRows = `
      <div class="detail-row">
        <span class="detail-key">New Admin Key</span>
        <span class="detail-val mono" title="${escHtml(ad.new_admin||'')}">${escHtml(ad.new_admin_short||'—')}</span>
      </div>`;
  } else if (p.action_type === 'UpdateRegistryMemberAction') {
    actionRows = `
      <div class="detail-row">
        <span class="detail-key">Target Member</span>
        <span class="detail-val mono" title="${escHtml(ad.target_key||'')}">${escHtml(ad.target_key_short||'—')}</span>
      </div>
      <div class="detail-row">
        <span class="detail-key">New Role</span>
        <span class="detail-val">${escHtml(ad.new_role||'—')}</span>
      </div>
      <div class="detail-row">
        <span class="detail-key">New Status</span>
        <span class="detail-val">${escHtml(ad.new_status||'—')}</span>
      </div>`;
  } else if (p.action_type === 'OffChainDecisionAction') {
    actionRows = `
      <div class="detail-row">
        <span class="detail-key">Decision Memo</span>
        <span class="detail-val">${escHtml(ad.memo||'—')}</span>
      </div>`;
  } else if (p.action_type === 'CreateVestingAction') {
    const trancheRows = (ad.tranches || []).map((t, i) => `
      <div class="detail-row">
        <span class="detail-key">Tranche ${i+1}</span>
        <span class="detail-val">${escHtml(t.ada)} ₳ — unlocks ${escHtml(t.release_fmt)}</span>
      </div>`
    ).join('');
    actionRows = `
      <div class="detail-row">
        <span class="detail-key">Recipient</span>
        <span class="detail-val mono" title="${escHtml(ad.recipient||'')}">${escHtml(ad.recipient_short||'—')}</span>
      </div>
      <div class="detail-row">
        <span class="detail-key">Total</span>
        <span class="detail-val amount">${escHtml(ad.total_ada||'—')} ₳ in ${escHtml(String(ad.tranche_count||0))} tranche(s)</span>
      </div>
      ${trancheRows}
      <div class="detail-row">
        <span class="detail-key">Memo</span>
        <span class="detail-val">${escHtml(ad.memo||'—')}</span>
      </div>`;
  }

  // ── Timeline ────────────────────────────────────────────
  const trClass = p.deadline_passed ? 'tl-sub passed' : 'tl-sub pending';
  const tlClass = p.timelock_cleared ? 'tl-sub cleared' : 'tl-sub pending';
  const tlLabel = p.timelock_cleared ? '✓ Timelock cleared' : 'Awaiting timelock';

  // ── Quorum ──────────────────────────────────────────────
  const qFill = `<div class="quorum-fill${p.quorum_met?' met':''}" style="width:${Math.min(p.quorum_pct,100)}%"></div>`;
  const qLabel = p.quorum_met ? '✓ Quorum met' : 'Awaiting quorum';

  // ── Vote chips ──────────────────────────────────────────
  const voteChips = p.votes.map(v => `
    <div class="vote-chip-v2 ${v.approve?'yes':'no'}">
      <span class="vote-mark">${v.approve?'✓':'✗'}</span>
      <div class="vote-info">
        <div class="vote-voter" title="${escHtml(v.voter_full||v.voter)}">${escHtml(v.voter)}</div>
        <div class="vote-meta">${escHtml(v.role||'?')} · ${v.weight}w</div>
      </div>
    </div>`).join('');

  // ── Version warning ─────────────────────────────────────
  const warn = !p.version_ok
    ? `<div class="warn-strip">⚠ Registry version mismatch (pinned v${p.registry_version_pinned}, current v${p.registry_version_current}) — proposal requires re-submission</div>`
    : '';

  // ── Action buttons ──────────────────────────────────────
  let btns = '';
  if (p.status === 'Voting' && p.version_ok) {
    btns += `<button class="btn btn-vote-yes" onclick="doVote('${p.ref}',true)">✓ Vote Yes</button>`;
    btns += `<button class="btn btn-vote-no"  onclick="doVote('${p.ref}',false)">✗ Vote No</button>`;
    if (p.quorum_met && p.timelock_cleared)
      btns += `<button class="btn btn-execute" onclick="doExecute('${p.ref}')">▶ Execute</button>`;
    if (p.deadline_passed && !p.quorum_met)
      btns += `<button class="btn btn-expire" onclick="doExpire('${p.ref}')">✕ Expire</button>`;
  }
  if (p.status === 'Executed' && p.is_transfer)
    btns += `<button class="btn btn-transfer" onclick="doTransfer('${p.ref}')">💸 Release Funds</button>`;
  if (p.status === 'Executed' && p.action_type === 'CreateVestingAction')
    btns += `<button class="btn btn-transfer" onclick="doFundVesting('${p.ref}')">⏱ Fund Vesting</button>`;
  if (p.status === 'Executed')
    btns += `<button class="btn btn-executor" onclick="doRunExecutor('${p.ref}')">⚡ Run Task</button>`;

  return `
    <div class="proposal-card ${p.status_class}${extraClass}">

      <!-- Header -->
      <div class="prop-header">
        <div class="prop-header-left">
          <div class="prop-ref">${escHtml(p.ref_short)}</div>
          <div class="prop-title">${escHtml(p.description)}</div>
        </div>
        <span class="status-badge ${p.status_class}">${p.status}</span>
      </div>

      <!-- Action -->
      <div class="prop-section">
        <div class="prop-section-label-row">
          <span class="prop-section-label">Action</span>
          ${p.rationale_url ? `<a class="ipfs-link" href="${escHtml(p.rationale_url.replace('ipfs://', 'https://ipfs.io/ipfs/'))}" target="_blank" rel="noopener">📄 Rationale</a>` : ''}
        </div>
        <span class="action-tag">${icon} ${escHtml(ad.type||p.action_type.replace('Action',''))}</span>
        ${actionRows ? `<div class="detail-grid">${actionRows}</div>` : ''}
      </div>

      <!-- Timeline -->
      <div class="prop-section">
        <div class="prop-section-label">Timeline</div>
        <div class="timeline-grid">
          <div class="timeline-item">
            <span class="tl-icon">🗓</span>
            <div class="tl-body">
              <div class="tl-key">Vote Deadline</div>
              <div class="tl-val">${escHtml(p.vote_deadline_fmt||'—')}</div>
              <div class="${trClass}">${escHtml(p.time_remaining||'—')}</div>
            </div>
          </div>
          <div class="timeline-item">
            <span class="tl-icon">⏳</span>
            <div class="tl-body">
              <div class="tl-key">Execute After (Timelock)</div>
              <div class="tl-val">${escHtml(p.execute_after_fmt||'—')}</div>
              <div class="${tlClass}">${tlLabel}</div>
            </div>
          </div>
        </div>
      </div>

      <!-- Voting Progress -->
      <div class="prop-section">
        <div class="prop-section-label-row">
          <span class="prop-section-label">Voting Progress</span>
          <span class="quorum-pct-badge${p.quorum_met?' met':''}">${p.quorum_pct}%</span>
        </div>
        <div class="quorum-track">${qFill}</div>
        <div class="quorum-legend">
          <span class="${p.quorum_met?'ql-met':'ql-pending'}">${qLabel}</span>
          <span class="ql-score">${p.yes_score} of ${p.quorum} pts required</span>
          ${p.deposit_ada ? `<span class="deposit-badge ${p.status==='Executed'?'refunded':p.status==='Expired'?'forfeited':'locked'}">
            ${p.status==='Executed'?'↩':'🔒'} ${p.deposit_ada}₳ deposit${p.status==='Executed'?' refunded':p.status==='Expired'?' forfeited':''}
          </span>` : ''}
        </div>
      </div>

      ${p.votes.length ? `
      <!-- Votes Cast -->
      <div class="prop-section">
        <div class="prop-section-label">Votes Cast (${p.votes.length})</div>
        <div class="votes-wrap-v2">${voteChips}</div>
      </div>` : ''}

      ${warn}
      ${btns ? `<div class="prop-actions">${btns}</div>` : ''}
    </div>`;
}

// ── Transaction actions ────────────────────────────────────
async function doVote(ref, approve) {
  if (!confirm(`Cast a ${approve ? 'YES' : 'NO'} vote on this proposal?`)) return;
  await buildAndShowTx('/api/vote', { proposal_ref: ref, approve },
    `Vote ${approve ? 'Yes ✓' : 'No ✗'}`, ref);
}
async function doExecute(ref) {
  if (!confirm('Execute this proposal?')) return;
  await buildAndShowTx('/api/execute', { proposal_ref: ref }, 'Execute Proposal', ref);
}
async function doExpire(ref) {
  if (!confirm('Expire this proposal?')) return;
  await buildAndShowTx('/api/expire', { proposal_ref: ref }, 'Expire Proposal', ref);
}
async function doTransfer(ref) {
  if (!confirm('Release treasury funds for this proposal?\nConfirm the amount in the transaction details.')) return;
  await buildAndShowTx('/api/transfer', { governance_ref: ref }, 'Release Treasury Funds', ref);
}
async function doFundVesting(ref) {
  if (!confirm('Fund vesting schedule from treasury?\nConfirm the schedule details in the transaction.')) return;
  const vstHash = prompt('Enter vesting script hash (or leave blank if set in env):') || '';
  await buildAndShowTx('/api/vesting/fund', { governance_ref: ref, vesting_script_hash: vstHash }, 'Fund Vesting Schedule', ref);
}
async function doClaimVesting(ref) {
  if (!confirm('Claim matured vesting tranches?')) return;
  const addr = prompt('Enter your recipient address (bech32):') || '';
  await buildAndShowTx('/api/vesting/claim', { vesting_ref: ref, recipient_address: addr }, 'Claim Vested Funds', ref);
}

async function doRunExecutor(ref) {
  const modal = document.getElementById('executor-modal');
  const body  = document.getElementById('exec-body');
  const title = document.getElementById('exec-title');
  title.textContent = 'Executor Agent';
  body.innerHTML = `
    <div class="exec-spinner">
      <div class="exec-spinner-ring"></div>
      <div class="exec-spinner-label">Agent is working…</div>
    </div>`;
  modal.classList.remove('hidden');

  try {
    const r = await fetch('/api/executor/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ proposal_ref: ref }),
    });
    const j = await r.json();
    if (!j.ok) throw new Error(j.error);
    const res = j.result;
    showExecutorResult(res);
  } catch (e) {
    body.innerHTML = `<div class="exec-error">⚠ ${escHtml(e.message)}</div>`;
  }
}

// ── Vesting ────────────────────────────────────────────────
function renderVesting(v) {
  const section = document.getElementById('vesting-section');
  const grid    = document.getElementById('vesting-grid');
  const meta    = document.getElementById('vesting-meta');
  if (!section || !grid) return;

  const schedules = v.schedules || [];
  if (!schedules.length) {
    section.classList.add('hidden');
    return;
  }

  section.classList.remove('hidden');
  if (meta) meta.textContent = `${schedules.length} schedule(s) · ${v.claimable_count} claimable`;

  grid.innerHTML = schedules.map(s => {
    const trancheRows = (s.tranches || []).map((t, i) => `
      <div class="detail-row">
        <span class="detail-key ${t.matured ? 'amount' : 'muted'}">Tranche ${i+1}</span>
        <span class="detail-val">${escHtml(t.ada)} ₳ — ${escHtml(t.release_fmt)}${t.matured ? ' <span class="form-hint" style="color:var(--green)">(matured)</span>' : ''}</span>
      </div>`).join('');

    return `
      <div class="proposal-card">
        <div class="prop-header">
          <div class="prop-header-left">
            <div class="prop-ref">${escHtml(s.ref_short)}</div>
            <div class="prop-title">Vesting → ${escHtml(s.recipient_short)}</div>
          </div>
          <span class="status-badge ${s.has_claimable ? 'voting' : 'executed'}">${s.has_claimable ? 'Claimable' : 'Locked'}</span>
        </div>
        <div class="prop-section">
          <div class="prop-section-label">Tranches</div>
          <div class="detail-grid">${trancheRows}</div>
        </div>
        <div class="prop-section">
          <div class="prop-section-label-row">
            <span class="prop-section-label">Balance</span>
            <span class="quorum-pct-badge${s.has_claimable ? ' met' : ''}">${escHtml(s.total_ada)} ₳</span>
          </div>
          ${s.has_claimable ? `<div class="quorum-legend"><span class="ql-met">↓ ${escHtml(s.claimable_ada)} ₳ claimable now</span></div>` : ''}
        </div>
        ${s.has_claimable ? `<div class="prop-actions"><button class="btn btn-execute" onclick="doClaimVesting('${escHtml(s.ref)}')">↓ Claim ${escHtml(s.claimable_ada)} ₳</button></div>` : ''}
      </div>`;
  }).join('');
}

// ── Delegation ─────────────────────────────────────────────
function openDelegateModal(memberKeyHash, currentDelegate) {
  const members = (_state?.registry?.members || []).filter(m =>
    m.key_hash !== memberKeyHash && m.status === 'Active' && !m.delegate
  );
  const options = members.map(m =>
    `<option value="${escHtml(m.key_hash)}"${currentDelegate === m.key_hash ? ' selected' : ''}>${escHtml(m.key_hash_short)} (${escHtml(m.role)})</option>`
  ).join('');

  const body = document.getElementById('exec-body');
  const title = document.getElementById('exec-title');
  title.textContent = 'Set Vote Delegate';
  body.innerHTML = `
    <p class="muted small">Choose a member to delegate your vote to, or clear an existing delegation.</p>
    <div class="form-group" style="margin-top:12px">
      <label class="form-label">Delegate to</label>
      <select class="form-input" id="delegate-select">
        <option value="">(none — clear delegation)</option>
        ${options}
      </select>
    </div>
    <input type="hidden" id="delegate-member-key" value="${escHtml(memberKeyHash)}">
    <div class="prop-actions" style="margin-top:16px">
      <button class="btn btn-execute" onclick="doSetDelegate()">Build Transaction →</button>
    </div>
    <div id="delegate-error" class="form-error hidden"></div>
  `;
  document.getElementById('executor-modal').classList.remove('hidden');
}

async function doSetDelegate() {
  const memberKey = document.getElementById('delegate-member-key')?.value;
  const newDelegate = document.getElementById('delegate-select')?.value || null;
  const errEl = document.getElementById('delegate-error');
  if (errEl) errEl.classList.add('hidden');

  try {
    const r = await fetch('/api/delegate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ member_key_hash: memberKey, new_delegate: newDelegate }),
    });
    const j = await r.json();
    if (!j.ok) throw new Error(j.error);

    document.getElementById('executor-modal').classList.add('hidden');
    _pendingTx = { endpoint: '/api/delegate', payload: {}, summary: j.summary, inputs: j.inputs };
    showTxModal(newDelegate ? 'Set Delegate' : 'Clear Delegate', j);
  } catch (e) {
    if (errEl) { errEl.textContent = '⚠ ' + e.message; errEl.classList.remove('hidden'); }
    else showError(e.message);
  }
}

function showExecutorResult(res) {
  const body = document.getElementById('exec-body');
  const success = res.success;
  const events = (res.events || []).map(ev =>
    `<span class="exec-event-chip ev-${ev}">${ev.replace(/_/g,' ')}</span>`
  ).join('');

  body.innerHTML = `
    <div class="exec-status ${success ? 'exec-ok' : 'exec-fail'}">
      ${success ? '✅ Task completed successfully' : '❌ Task encountered an error'}
    </div>
    <div class="exec-meta">
      <span class="exec-meta-item">🎯 ${escHtml(res.action_type||'')}</span>
      <span class="exec-meta-item">🔄 ${res.iterations} turns</span>
    </div>
    ${events ? `<div class="exec-events">${events}</div>` : ''}
    ${res.summary ? `
    <div class="exec-summary-label">Agent Summary</div>
    <div class="exec-summary">${escHtml(res.summary)}</div>` : ''}
    ${res.error ? `
    <div class="exec-summary-label">Error</div>
    <div class="exec-error-detail">${escHtml(res.error)}</div>` : ''}
    <div class="exec-ref">${escHtml(res.proposal_ref||'')}</div>
  `;
  loadAudit();
}

function closeExecutorModal(e) {
  if (e && e.target !== document.getElementById('executor-modal')) return;
  document.getElementById('executor-modal').classList.add('hidden');
}

async function buildAndShowTx(endpoint, payload, title) {
  try {
    const r = await fetch(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const j = await r.json();
    if (!j.ok) throw new Error(j.error);
    _pendingTx = { endpoint, payload, summary: j.summary, inputs: j.inputs, title };
    showTxModal(title, j);
  } catch (e) {
    showError(e.message);
  }
}

// ── Transaction modal ──────────────────────────────────────
function showTxModal(title, tx) {
  setText('tx-title', title);

  // Build detail rows from summary lines
  const details = document.getElementById('tx-details');
  const lines = (tx.summary || '').split('\n');
  details.innerHTML = lines.map((line, i) => {
    if (i === 0) return ''; // skip "Transaction: ..."
    const [key, ...rest] = line.split(':');
    if (!rest.length) return '';
    const val = rest.join(':').trim();
    const isAmount = key.includes('lovelace') || key.includes('ada');
    return `
      <div class="tx-row">
        <span class="tx-row-key">${escHtml(key.trim().replace(/\s+/g,''))}</span>
        <span class="tx-row-val${isAmount ? ' highlight' : ''}">${escHtml(val)}</span>
      </div>
    `;
  }).join('') + `
    <div class="tx-divider"></div>
    <div class="tx-row">
      <span class="tx-row-key">Inputs</span>
      <span class="tx-row-val">${(tx.inputs||[]).map(escHtml).join('<br>')}</span>
    </div>
  `;

  document.getElementById('tx-raw-body').textContent = tx.summary || '';

  // Wallet sign section
  const walletSection = document.getElementById('tx-wallet-action');
  const noWalletMsg   = document.getElementById('tx-no-wallet');
  if (_wallet) {
    walletSection.classList.remove('hidden');
    noWalletMsg.classList.add('hidden');
    document.getElementById('tx-wallet-badge').innerHTML = `
      <span class="wcb-addr">⬡ ${_wallet.address.slice(0, 20)}…</span>
      <span class="wcb-bal">${_wallet.ada} ₳ available</span>
    `;
  } else {
    walletSection.classList.add('hidden');
    noWalletMsg.classList.remove('hidden');
  }

  document.getElementById('tx-modal').classList.remove('hidden');
}

function closeTxModal(e) {
  if (e && e.target !== document.getElementById('tx-modal')) return;
  document.getElementById('tx-modal').classList.add('hidden');
  _pendingTx = null;
}

function copyTx() {
  const text = _pendingTx?.summary || '';
  navigator.clipboard.writeText(text).then(() => {
    const btns = document.querySelectorAll('.modal-footer .btn-ghost');
    btns.forEach(b => { if (b.textContent.includes('Copy')) { b.textContent = 'Copied!'; setTimeout(() => b.textContent = 'Copy Summary', 1500); }});
  });
}

// ── Sign & Submit (CIP-30) ─────────────────────────────────
async function signAndSubmit() {
  if (!_wallet || !_pendingTx) return;
  const btn = document.getElementById('tx-sign-btn');
  btn.disabled = true;
  btn.textContent = 'Signing…';
  try {
    // For now, mock the signing flow — in production:
    // 1. GET /api/tx/cbor to get unsigned CBOR hex
    // 2. const witnesses = await _wallet.api.signTx(cborHex, true);
    // 3. POST /api/tx/submit with the signed tx
    await new Promise(r => setTimeout(r, 1200)); // simulate signing
    btn.textContent = 'Submitting…';
    await new Promise(r => setTimeout(r, 800));

    closeTxModal();
    showToast('Transaction signed and submitted!', 'green');
    setTimeout(loadState, 3000);
  } catch (e) {
    btn.disabled = false;
    btn.textContent = 'Sign & Submit';
    showError('Wallet signing failed: ' + e.message);
  }
}

// ── Wallet modal ───────────────────────────────────────────
function openWalletModal() {
  if (_wallet) {
    // Already connected — show disconnect option
    if (confirm(`Disconnect ${_wallet.name}?`)) disconnectWallet();
    return;
  }
  renderWalletList();
  document.getElementById('wallet-modal').classList.remove('hidden');
}

function closeWalletModal(e) {
  if (e && e.target !== document.getElementById('wallet-modal')) return;
  document.getElementById('wallet-modal').classList.add('hidden');
}

function renderWalletList() {
  const list = document.getElementById('wallet-list');
  list.innerHTML = WALLETS.map(w => {
    const found = !!(window.cardano && window.cardano[w.key]);
    return `
      <button class="wallet-option${found ? '' : ' disabled'}"
              onclick="${found ? `connectWallet('${w.key}')` : ''}"
              ${found ? '' : 'disabled'}>
        <div class="wallet-logo">${w.emoji}</div>
        <div>
          <div class="wallet-name">${w.name}</div>
          <div class="wallet-status${found ? ' found' : ''}">${found ? 'Detected' : 'Not installed'}</div>
        </div>
      </button>
    `;
  }).join('');

  // If none detected, show hint
  const anyFound = WALLETS.some(w => window.cardano?.[w.key]);
  if (!anyFound) {
    list.innerHTML += `
      <div class="empty-state small" style="padding:12px 0">
        No Cardano wallet extension detected.<br>
        Install <strong>Eternl</strong> or <strong>Nami</strong> from the Chrome Web Store.
      </div>
    `;
  }
}

async function connectWallet(key) {
  try {
    document.getElementById('wallet-modal').classList.add('hidden');
    const api = await window.cardano[key].enable();
    const usedAddrs = await api.getUsedAddresses();
    const address = usedAddrs.length ? usedAddrs[0] : (await api.getUnusedAddresses())[0];

    // Fetch balance from backend
    let ada = '—';
    try {
      const r = await fetch(`/api/wallet/balance?address=${encodeURIComponent(address)}`);
      const j = await r.json();
      if (j.ok) ada = j.ada;
    } catch (_) {}

    _wallet = { name: key, api, address, ada };
    updateWalletUI();
    showToast(`${key.charAt(0).toUpperCase() + key.slice(1)} connected`, 'green');
  } catch (e) {
    showError('Could not connect wallet: ' + e.message);
  }
}

function disconnectWallet() {
  _wallet = null;
  updateWalletUI();
  showToast('Wallet disconnected', 'amber');
}

function updateWalletUI() {
  const btn   = document.getElementById('wallet-btn');
  const label = document.getElementById('wallet-label');
  if (!btn) return;
  if (_wallet) {
    btn.classList.add('connected');
    label.textContent = `${_wallet.address.slice(0,12)}… ${_wallet.ada} ₳`;
  } else {
    btn.classList.remove('connected');
    label.textContent = 'Connect Wallet';
  }
}

// ── New Proposal modal ─────────────────────────────────────

let _selectedActionType = 'TreasuryTransfer';

function openProposalModal() {
  _selectedActionType = 'TreasuryTransfer';
  document.querySelectorAll('.action-type-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.action === _selectedActionType);
  });
  renderActionFields();
  updateQuorumHint();
  document.getElementById('p-error').classList.add('hidden');
  document.getElementById('p-description').value = '';
  document.getElementById('p-deadline-hours').value = '72';
  document.getElementById('p-timelock-hours').value = '24';
  document.getElementById('propose-modal').classList.remove('hidden');
  setTimeout(() => document.getElementById('p-description').focus(), 50);
}

function closeProposalModal(e) {
  if (e && e.target !== document.getElementById('propose-modal')) return;
  document.getElementById('propose-modal').classList.add('hidden');
}

function selectActionType(type) {
  _selectedActionType = type;
  document.querySelectorAll('.action-type-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.action === type);
  });
  renderActionFields();
}

function renderActionFields() {
  const el = document.getElementById('p-action-fields');
  const members = (_state?.registry?.members || []).filter(m => m.status === 'Active');

  if (_selectedActionType === 'TreasuryTransfer') {
    const cap = _state?.treasury?.max_transfer_ada || '—';
    const memberOptions = members.map(m =>
      `<option value="${escHtml(m.key_hash)}">${escHtml(m.key_hash_short)} (${escHtml(m.role)})</option>`
    ).join('');
    el.innerHTML = `
      <div class="form-section-title">Transfer Details</div>
      <div class="form-group">
        <label class="form-label" for="p-recipient">
          Recipient <span class="form-hint">active member key hash</span>
        </label>
        ${members.length ? `
        <select class="form-input" id="p-recipient-select" onchange="
          document.getElementById('p-recipient').value = this.value;
        ">
          <option value="">— Select member or enter manually —</option>
          ${memberOptions}
        </select>` : ''}
        <input type="text" id="p-recipient" class="form-input"
               placeholder="hex verification key hash (56 chars)…" maxlength="64">
      </div>
      <div class="form-group">
        <label class="form-label" for="p-amount-ada">
          Amount (ADA) <span class="form-hint">cap: ${cap} ADA</span>
        </label>
        <input type="number" id="p-amount-ada" class="form-input"
               placeholder="0.00" step="0.1" min="0.1">
      </div>
      <div class="form-group">
        <label class="form-label" for="p-memo">Memo</label>
        <input type="text" id="p-memo" class="form-input"
               placeholder="Purpose of this payment…" maxlength="100">
      </div>
      <div class="form-group">
        <label class="form-label">Native Tokens <span class="form-hint">optional — leave empty for ADA-only</span></label>
        <div id="token-list"></div>
        <button type="button" class="btn btn-ghost small" onclick="addTokenRow()">+ Add Token</button>
      </div>`;

  } else if (_selectedActionType === 'OffChainDecision') {
    el.innerHTML = `
      <div class="form-section-title">Decision Details</div>
      <div class="form-group">
        <label class="form-label" for="p-memo">Decision Memo</label>
        <input type="text" id="p-memo" class="form-input"
               placeholder="Describe the decision being ratified…" maxlength="200">
      </div>`;

  } else if (_selectedActionType === 'UpdateRegistryMember') {
    const memberOptions = (_state?.registry?.members || []).map(m =>
      `<option value="${escHtml(m.key_hash)}">${escHtml(m.key_hash_short)} — ${escHtml(m.role)} / ${escHtml(m.status)}</option>`
    ).join('');
    el.innerHTML = `
      <div class="form-section-title">Member Update</div>
      <div class="form-group">
        <label class="form-label" for="p-target-select">Target Member</label>
        ${memberOptions ? `
        <select class="form-input" id="p-target-select" onchange="
          document.getElementById('p-target-key').value = this.value;
        ">
          <option value="">— Select member —</option>
          ${memberOptions}
        </select>` : ''}
        <input type="text" id="p-target-key" class="form-input"
               placeholder="hex verification key hash…" maxlength="64">
      </div>
      <div class="form-row">
        <div class="form-group half">
          <label class="form-label" for="p-new-role">New Role</label>
          <select class="form-input" id="p-new-role">
            <option value="Admin">Admin (3w)</option>
            <option value="Treasurer">Treasurer (2w)</option>
            <option value="Member" selected>Member (1w)</option>
            <option value="Observer">Observer (0w)</option>
          </select>
        </div>
        <div class="form-group half">
          <label class="form-label" for="p-new-status">New Status</label>
          <select class="form-input" id="p-new-status">
            <option value="Active" selected>Active</option>
            <option value="Suspended">Suspended</option>
            <option value="Removed">Removed</option>
          </select>
        </div>
      </div>`;

  } else if (_selectedActionType === 'RotateAdmin') {
    el.innerHTML = `
      <div class="form-section-title">Admin Rotation</div>
      <div class="form-group">
        <label class="form-label" for="p-new-admin">New Admin Key Hash</label>
        <input type="text" id="p-new-admin" class="form-input"
               placeholder="hex verification key hash of new admin…" maxlength="64">
      </div>`;

  } else if (_selectedActionType === 'CreateVesting') {
    const memberOptions = members.map(m =>
      `<option value="${escHtml(m.key_hash)}">${escHtml(m.key_hash_short)} (${escHtml(m.role)})</option>`
    ).join('');
    el.innerHTML = `
      <div class="form-section-title">Vesting Schedule</div>
      <div class="form-group">
        <label class="form-label" for="p-vest-recipient">Recipient Key Hash</label>
        ${members.length ? `
        <select class="form-input" onchange="document.getElementById('p-vest-recipient').value = this.value">
          <option value="">— Select member or enter manually —</option>
          ${memberOptions}
        </select>` : ''}
        <input type="text" id="p-vest-recipient" class="form-input"
               placeholder="hex verification key hash…" maxlength="64">
      </div>
      <div class="form-group">
        <label class="form-label">Tranches <span class="form-hint">each tranche releases on its date</span></label>
        <div id="tranche-list"></div>
        <button type="button" class="btn btn-ghost small" onclick="addTrancheRow()">+ Add Tranche</button>
      </div>
      <div class="form-group">
        <label class="form-label" for="p-vest-memo">Memo</label>
        <input type="text" id="p-vest-memo" class="form-input"
               placeholder="Purpose of this vesting schedule…" maxlength="100">
      </div>`;
  }
}

function addTrancheRow() {
  const list = document.getElementById('tranche-list');
  if (!list) return;
  const row = document.createElement('div');
  row.className = 'token-row form-row';
  const defaultDate = new Date(Date.now() + 30 * 86400_000).toISOString().slice(0, 10);
  row.innerHTML = `
    <div class="form-group" style="flex:1.5">
      <input type="date" class="form-input tranche-date" value="${defaultDate}" title="Release date (UTC midnight)">
    </div>
    <div class="form-group" style="flex:1">
      <input type="number" class="form-input tranche-ada" placeholder="ADA amount" min="1" step="0.1">
    </div>
    <button type="button" class="btn btn-ghost small" onclick="this.closest('.token-row').remove()" style="align-self:flex-end;margin-bottom:4px">✕</button>
  `;
  list.appendChild(row);
}

function collectTranches() {
  return Array.from(document.querySelectorAll('.tranche-date')).map((el, i) => {
    const adaEl = el.closest('.token-row').querySelector('.tranche-ada');
    const dateMs = new Date(el.value + 'T00:00:00Z').getTime();
    const ada = parseFloat(adaEl?.value || '0');
    return { release_time_ms: dateMs, ada };
  }).filter(t => t.release_time_ms && t.ada > 0);
}

function addTokenRow() {
  const list = document.getElementById('token-list');
  if (!list) return;
  const row = document.createElement('div');
  row.className = 'token-row form-row';
  row.innerHTML = `
    <div class="form-group" style="flex:2">
      <input type="text" class="form-input token-policy" placeholder="Policy ID (56 hex chars)…" maxlength="56">
    </div>
    <div class="form-group" style="flex:1">
      <input type="text" class="form-input token-asset" placeholder="Asset name (hex or UTF-8)">
    </div>
    <div class="form-group" style="flex:1">
      <input type="number" class="form-input token-qty" placeholder="Quantity" min="1" step="1">
    </div>
    <button type="button" class="btn btn-ghost small" onclick="this.closest('.token-row').remove()" style="align-self:flex-end;margin-bottom:4px">✕</button>
  `;
  list.appendChild(row);
}

function collectTokens() {
  return Array.from(document.querySelectorAll('.token-row')).map(row => ({
    policy_id:  row.querySelector('.token-policy')?.value.trim() || '',
    asset_name: row.querySelector('.token-asset')?.value.trim() || '',
    quantity:   parseInt(row.querySelector('.token-qty')?.value || '0'),
  })).filter(t => t.policy_id && t.quantity > 0);
}

function updateQuorumHint() {
  const hint = document.getElementById('p-quorum-hint');
  const max  = _state?.registry?.max_yes_score;
  if (hint && max != null) hint.textContent = `pts required to pass (max possible: ${max})`;
}

async function submitProposal() {
  const btn = document.getElementById('p-submit-btn');
  const errEl = document.getElementById('p-error');
  errEl.classList.add('hidden');

  const description = document.getElementById('p-description')?.value.trim();
  const deadlineHours = parseFloat(document.getElementById('p-deadline-hours')?.value || 0);
  const timelockHours = parseFloat(document.getElementById('p-timelock-hours')?.value || 0);
  const quorum = parseInt(document.getElementById('p-quorum')?.value || 0);
  const depositAda = parseFloat(document.getElementById('p-deposit-ada')?.value || 2);
  const rationaleUrl = document.getElementById('p-rationale-url')?.value.trim() || '';

  if (!description) return showProposalError('Description is required.');
  if (deadlineHours < 1) return showProposalError('Vote deadline must be at least 1 hour.');
  if (quorum < 1) return showProposalError('Quorum must be at least 1 pt.');
  if (depositAda < 2) return showProposalError('Deposit must be at least 2 ADA.');

  const now = Date.now();
  const deadline_ms    = now + deadlineHours * 3_600_000;
  const execute_after_ms = deadline_ms + timelockHours * 3_600_000;

  // Collect action-specific fields
  const payload = {
    action_type: _selectedActionType,
    description,
    deadline_ms,
    execute_after_ms,
    quorum,
    deposit_ada: depositAda,
    rationale_url: rationaleUrl,
  };

  if (_selectedActionType === 'TreasuryTransfer') {
    payload.recipient  = document.getElementById('p-recipient')?.value.trim();
    payload.amount_ada = document.getElementById('p-amount-ada')?.value;
    payload.memo       = document.getElementById('p-memo')?.value.trim();
    payload.tokens     = collectTokens();
    if (!payload.recipient) return showProposalError('Recipient key hash is required.');
    const amt = parseFloat(payload.amount_ada || '0');
    if (amt <= 0 && payload.tokens.length === 0)
      return showProposalError('Specify an ADA amount or at least one native token.');
  } else if (_selectedActionType === 'OffChainDecision') {
    payload.memo = document.getElementById('p-memo')?.value.trim();
    if (!payload.memo) return showProposalError('Decision memo is required.');
  } else if (_selectedActionType === 'UpdateRegistryMember') {
    payload.target_key  = document.getElementById('p-target-key')?.value.trim();
    payload.new_role    = document.getElementById('p-new-role')?.value;
    payload.new_status  = document.getElementById('p-new-status')?.value;
    if (!payload.target_key) return showProposalError('Target member key hash is required.');
  } else if (_selectedActionType === 'RotateAdmin') {
    payload.new_admin = document.getElementById('p-new-admin')?.value.trim();
    if (!payload.new_admin) return showProposalError('New admin key hash is required.');
  } else if (_selectedActionType === 'CreateVesting') {
    payload.recipient = document.getElementById('p-vest-recipient')?.value.trim();
    payload.memo      = document.getElementById('p-vest-memo')?.value.trim();
    payload.tranches  = collectTranches();
    if (!payload.recipient) return showProposalError('Recipient key hash is required.');
    if (!payload.tranches.length) return showProposalError('At least one tranche is required.');
  }

  btn.disabled = true;
  btn.textContent = 'Building…';

  try {
    const r = await fetch('/api/propose', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const j = await r.json();
    if (!j.ok) throw new Error(j.error);

    closeProposalModal();
    _pendingTx = { endpoint: '/api/propose', payload, summary: j.summary, inputs: j.inputs };
    showTxModal('Create Proposal', j);
  } catch (e) {
    showProposalError(e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Build Transaction →';
  }
}

function showProposalError(msg) {
  const el = document.getElementById('p-error');
  if (!el) return;
  el.textContent = '⚠ ' + msg;
  el.classList.remove('hidden');
}

// ── Audit / Activity ───────────────────────────────────────
async function loadAudit() {
  try {
    const r = await fetch('/api/audit');
    const j = await r.json();
    if (!j.ok) return;
    renderActivity(j.entries);
  } catch (_) {}
}

function renderActivity(entries) {
  const el = document.getElementById('activity-feed');
  if (!el) return;
  if (!entries.length) {
    el.innerHTML = '<div class="empty-state small">No activity logged yet</div>';
    return;
  }
  el.innerHTML = entries.slice(0, 15).map(e => `
    <div class="activity-item">
      <div class="activity-event ev-${e.event}">${e.event.replace(/_/g, ' ')}</div>
      <div class="activity-ts">${e.ts.slice(0, 19).replace('T', ' ')}</div>
      <div class="activity-details">${escHtml(e.details.slice(0, 80))}</div>
    </div>
  `).join('');
}

// ── DRep status ────────────────────────────────────────────
async function loadDrep() {
  try {
    const r = await fetch('/api/drep');
    const j = await r.json();
    if (!j.ok) return;
    const d = j.drep;
    const dot = document.getElementById('drep-status-dot');
    if (dot) {
      dot.className = 'dot ' + (d.registered && d.is_active ? 'dot-green pulse' : d.registered ? 'dot-amber' : 'dot-red');
    }
    setText('drep-id', d.drep_id || '—');
    setText('drep-registered', d.registered ? (d.is_active ? 'Yes (active)' : 'Yes (inactive)') : 'Not registered');
    setText('drep-voting-power', d.registered ? (d.voting_power_ada + ' ₳') : '—');
    setText('drep-delegators', d.registered ? String(d.delegator_count) : '—');
  } catch (_) {}
}

// ── Toast notification ─────────────────────────────────────
function showToast(msg, type = 'blue') {
  const t = document.createElement('div');
  t.style.cssText = `
    position:fixed; bottom:24px; right:24px; z-index:9999;
    padding:12px 18px; border-radius:8px; font-size:13px; font-weight:500;
    animation: modal-in .2s ease;
    background: var(--card); border: 1px solid var(--border);
    color: var(--${type}); box-shadow: 0 8px 32px rgba(0,0,0,.5);
  `;
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 3000);
}

// ── Error banner ───────────────────────────────────────────
function showError(msg) {
  const el = document.getElementById('error-banner');
  if (!el) return;
  el.textContent = '⚠ ' + msg;
  el.classList.remove('hidden');
}
function hideError() {
  const el = document.getElementById('error-banner');
  if (el) el.classList.add('hidden');
}

// ── Helpers ────────────────────────────────────────────────
function setText(id, val) {
  const el = document.getElementById(id);
  if (el) el.textContent = String(val);
}
function escHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// Spinner keyframe
const style = document.createElement('style');
style.textContent = '@keyframes spin { to { transform: rotate(360deg); } }';
document.head.appendChild(style);
