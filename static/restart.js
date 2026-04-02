/* restart.js — Restart orchestrator UI */

// Track active restart for progress display
let _activeRestartId = null;

function showRestartDialog() {
    let existing = document.getElementById('restart-modal');
    if (existing) existing.remove();

    const modal = document.createElement('div');
    modal.id = 'restart-modal';
    modal.className = 'session-launcher-overlay';
    modal.onclick = (e) => { if (e.target === modal) modal.remove(); };

    modal.innerHTML = `
        <div class="session-launcher-dialog" style="width:380px">
            <div class="session-launcher-header">
                <span>Restart</span>
                <button onclick="this.closest('.session-launcher-overlay').remove()" aria-label="Close">&times;</button>
            </div>
            <div class="restart-body">
                <div class="restart-field">
                    <label class="restart-label">Scope</label>
                    <div class="restart-radios">
                        <label class="restart-radio">
                            <input type="radio" name="restart-scope" value="agents" checked>
                            <span>Agents only</span>
                        </label>
                        <label class="restart-radio">
                            <input type="radio" name="restart-scope" value="server">
                            <span>Server only</span>
                        </label>
                        <label class="restart-radio">
                            <input type="radio" name="restart-scope" value="everything">
                            <span>Everything</span>
                        </label>
                    </div>
                </div>
                <div class="restart-field">
                    <label class="restart-label" for="restart-reason">Reason</label>
                    <select id="restart-reason" class="restart-select">
                        <option value="refresh">Refresh</option>
                        <option value="bug">Bug</option>
                        <option value="config change">Config change</option>
                        <option value="custom">Custom...</option>
                    </select>
                    <input type="text" id="restart-reason-custom" class="restart-input hidden"
                           placeholder="Describe the reason...">
                </div>
                <div class="restart-field">
                    <label class="restart-check">
                        <input type="checkbox" id="restart-dry-run">
                        <span>Dry run (preview only, no changes)</span>
                    </label>
                </div>
                <div id="restart-progress" class="restart-progress hidden"></div>
                <div id="restart-error" class="restart-error hidden"></div>
            </div>
            <div class="restart-footer">
                <button class="restart-cancel"
                        onclick="this.closest('.session-launcher-overlay').remove()">Cancel</button>
                <button class="restart-confirm" id="restart-confirm-btn"
                        onclick="executeRestart()">Restart</button>
            </div>
        </div>`;

    // Wire up scope danger styling
    const radios = modal.querySelectorAll('input[name="restart-scope"]');
    const confirmBtn = modal.querySelector('#restart-confirm-btn');
    radios.forEach(r => r.addEventListener('change', () => {
        const isEverything = modal.querySelector('input[name="restart-scope"]:checked')?.value === 'everything';
        confirmBtn.classList.toggle('restart-confirm-danger', isEverything);
        confirmBtn.textContent = isEverything ? 'Restart Everything' : 'Restart';
    }));

    // Wire up custom reason toggle
    const sel = modal.querySelector('#restart-reason');
    const custom = modal.querySelector('#restart-reason-custom');
    sel.onchange = () => {
        custom.classList.toggle('hidden', sel.value !== 'custom');
        if (sel.value === 'custom') custom.focus();
    };

    document.body.appendChild(modal);
}

async function executeRestart() {
    const modal = document.getElementById('restart-modal');
    if (!modal) return;

    const scope = modal.querySelector('input[name="restart-scope"]:checked')?.value;
    const reasonSel = modal.querySelector('#restart-reason');
    const reasonCustom = modal.querySelector('#restart-reason-custom');
    const reason = reasonSel.value === 'custom'
        ? reasonCustom.value.trim() || 'no reason given'
        : reasonSel.value;
    const dryRun = modal.querySelector('#restart-dry-run')?.checked || false;
    const username = (document.getElementById('setting-username')?.value || 'user').trim();

    const btn = modal.querySelector('#restart-confirm-btn');
    btn.disabled = true;
    btn.textContent = dryRun ? 'Running dry run...' : 'Restarting...';

    const progress = modal.querySelector('#restart-progress');
    const errorDiv = modal.querySelector('#restart-error');
    progress.classList.remove('hidden');
    progress.textContent = 'Initiating...';
    errorDiv.classList.add('hidden');

    try {
        const resp = await fetch('/api/restart', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-Session-Token': window.__SESSION_TOKEN__ || '',
            },
            body: JSON.stringify({ scope, reason, dry_run: dryRun, initiated_by: username }),
        });
        const data = await resp.json();
        if (!resp.ok) {
            errorDiv.textContent = data.error || 'Restart failed';
            errorDiv.classList.remove('hidden');
            btn.disabled = false;
            btn.textContent = 'Retry';
            return;
        }

        _activeRestartId = data.restart_id;

        if (dryRun) {
            progress.innerHTML = '<strong>Dry run complete.</strong> ' +
                `Roster: ${(data.roster || []).map(a => a.name).join(', ') || 'none'}`;
            btn.textContent = 'Done';
            btn.onclick = () => modal.remove();
            btn.disabled = false;
        } else {
            progress.textContent = 'Restart in progress...';
            // Progress updates come via WebSocket — see handleRestartProgress
        }
    } catch (err) {
        errorDiv.textContent = `Network error: ${err.message}`;
        errorDiv.classList.remove('hidden');
        btn.disabled = false;
        btn.textContent = 'Retry';
    }
}

function handleRestartProgress(msg) {
    if (msg.restart_id !== _activeRestartId) return;

    const modal = document.getElementById('restart-modal');
    if (!modal) return;

    const progress = modal.querySelector('#restart-progress');
    if (!progress) return;

    const labels = {
        grace: 'Waiting for agents to save state...',
        killing: 'Stopping agent sessions...',
        restarting_server: 'Server restarting. Page will reconnect...',
        resurrecting: 'Starting agents back up...',
        complete: 'Restart complete.',
    };

    const label = labels[msg.phase] || msg.phase;
    const detail = msg.detail ? ` ${msg.detail}` : '';
    progress.textContent = `${label}${detail}`;

    if (msg.phase === 'complete' || msg.phase === 'partial_failed') {
        const btn = modal.querySelector('#restart-confirm-btn');
        if (btn) {
            btn.textContent = 'Done';
            btn.onclick = () => modal.remove();
            btn.disabled = false;
        }
        _activeRestartId = null;
    }
    if (msg.phase === 'restarting_server') {
        // Server will go down; show a refresh fallback after timeout
        setTimeout(() => {
            if (!progress) return;
            if (progress.textContent.includes('Server restarting')) {
                progress.innerHTML += '<br><button class="restart-confirm" onclick="location.reload()" style="margin-top:8px">Refresh page</button>';
            }
        }, 15000);
    }
}
