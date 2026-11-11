(() => {
  const root = document.documentElement;
  root.classList.add('js');
  const themeButtons = document.querySelectorAll('.theme-toggle');
  function setTheme(theme) {
    root.dataset.theme = theme;
    themeButtons.forEach(button => {
      const label = theme === 'dark' ? '切换为浅色主题' : '切换为深色主题';
      button.setAttribute('aria-label', label);
      button.title = label;
    });
  }
  try {
    setTheme(localStorage.getItem('forkiq-theme') === 'dark' ? 'dark' : 'light');
  } catch {
    setTheme('light');
  }
  themeButtons.forEach(button => button.addEventListener('click', () => {
    const theme = root.dataset.theme === 'dark' ? 'light' : 'dark';
    setTheme(theme);
    try { localStorage.setItem('forkiq-theme', theme); } catch { /* Storage is optional. */ }
  }));

  const menu = document.querySelector('.menu-toggle');
  const sidebar = document.querySelector('.sidebar');
  const overlay = document.querySelector('.sidebar-overlay');
  const mobile = window.matchMedia('(max-width: 760px)');
  function closeMenu() {
    document.body.classList.remove('nav-open');
    if (overlay) overlay.hidden = true;
    if (menu) {
      menu.setAttribute('aria-expanded', 'false');
      menu.setAttribute('aria-label', '打开导航');
    }
    if (sidebar) sidebar.inert = mobile.matches;
  }
  function openMenu() {
    document.body.classList.add('nav-open');
    overlay.hidden = false;
    sidebar.inert = false;
    menu.setAttribute('aria-expanded', 'true');
    menu.setAttribute('aria-label', '关闭导航');
    sidebar.querySelector('a').focus();
  }
  if (menu && sidebar && overlay) {
    closeMenu();
    menu.addEventListener('click', () => {
      if (document.body.classList.contains('nav-open')) closeMenu(); else openMenu();
    });
    overlay.addEventListener('click', () => { closeMenu(); menu.focus(); });
    mobile.addEventListener('change', closeMenu);
    document.addEventListener('keydown', event => {
      if (!document.body.classList.contains('nav-open')) return;
      if (event.key === 'Escape') { closeMenu(); menu.focus(); }
      if (event.key === 'Tab') {
        const elements = [...sidebar.querySelectorAll('a, button')];
        const first = elements[0], last = elements[elements.length - 1];
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
      }
    });
  }

  // Timestamps are stored in UTC. The server renders a short UTC form so the
  // page works without scripting; the browser re-renders every stamp in the
  // viewer's own timezone, including in fragments swapped in later.
  const pad = value => String(value).padStart(2, '0');
  function localizeTimes(root) {
    (root || document).querySelectorAll('time.ts[datetime]').forEach(element => {
      const when = new Date(element.getAttribute('datetime'));
      if (Number.isNaN(when.getTime())) return;
      element.textContent = pad(when.getMonth() + 1) + '-' + pad(when.getDate()) + ' ' +
        pad(when.getHours()) + ':' + pad(when.getMinutes());
      element.title = when.toLocaleString();
    });
  }
  localizeTimes();

  const errorDialog = document.querySelector('#error-dialog');
  if (errorDialog && typeof errorDialog.showModal === 'function') {
    const title = errorDialog.querySelector('#error-dialog-title');
    const repo = errorDialog.querySelector('#error-dialog-repo');
    const message = errorDialog.querySelector('#error-dialog-message');
    const summary = errorDialog.querySelector('#error-dialog-summary');
    const action = errorDialog.querySelector('#error-dialog-action');
    const technical = errorDialog.querySelector('#error-dialog-technical');
    const close = errorDialog.querySelector('[data-error-close]');
    let trigger = null;
    document.addEventListener('click', event => {
      const link = event.target.closest?.('a.error-details');
      if (!link || event.defaultPrevented || event.button > 0 ||
          event.ctrlKey || event.metaKey || event.shiftKey || event.altKey ||
          (link.getAttribute('target') && link.getAttribute('target') !== '_self') ||
          link.hasAttribute('download')) return;
      event.preventDefault();
      if (errorDialog.open) return;
      trigger = link;
      title.textContent = link.dataset.errorStatus ? '状态详情 · ' + link.dataset.errorStatus : '状态详情';
      repo.textContent = link.dataset.errorRepo || '';
      message.textContent = link.dataset.errorMessage || '';
      summary.textContent = link.dataset.errorSummary || '';
      action.textContent = link.dataset.errorAction || '';
      summary.hidden = !summary.textContent;
      action.hidden = !action.textContent;
      technical.open = !summary.textContent;
      errorDialog.setAttribute('aria-describedby', summary.textContent ?
        'error-dialog-repo error-dialog-summary error-dialog-action' : 'error-dialog-repo error-dialog-message');
      errorDialog.showModal();
    });
    close.addEventListener('click', () => errorDialog.close());
    errorDialog.addEventListener('click', event => {
      if (event.target !== errorDialog) return;
      const bounds = errorDialog.getBoundingClientRect();
      if (event.clientX < bounds.left || event.clientX > bounds.right ||
          event.clientY < bounds.top || event.clientY > bounds.bottom) errorDialog.close();
    });
    errorDialog.addEventListener('close', () => {
      if (trigger?.isConnected) trigger.focus();
      trigger = null;
    });
  }

  const bulkForm = document.querySelector('#repo-bulk-form');
  const bulkDialog = document.querySelector('#bulk-dialog');
  let updateSelection = () => {};
  if (bulkForm) {
    const selectAll = document.querySelector('[data-select-all]');
    const clear = document.querySelector('[data-clear-selection]');
    const count = document.querySelector('#bulk-count');
    const help = document.querySelector('#bulk-selection-help');
    const action = document.querySelector('#bulk-action');
    const actionButtons = [...bulkForm.querySelectorAll('[data-bulk-action]')];
    let preview = null;
    const confirm = bulkDialog?.querySelector('[data-bulk-confirm]');
    const checks = () => [...document.querySelectorAll('[data-repo-select]')];
    const selected = () => checks().filter(input => input.checked && !input.disabled);
    const labels = { pause: '暂停同步', resume: '恢复同步', sync: '安排同步', protect: '设为独立维护', remove: '移出管理' };
    const explanations = {
      pause: '停止后续上游同步，不影响仓库自身的 GitHub Actions。',
      resume: '按仓库原模式恢复；强制同步仓库仍会覆盖独有提交。独立维护的仓库会跳过。',
      sync: '按仓库原模式安排同步；强制同步仓库仍会覆盖独有提交。暂停的仓库需要先恢复。',
      protect: '停止上游同步并设为独立维护。之后的批量恢复不会解除这项保护。',
      remove: '只移出面板，不删除 GitHub 仓库和服务器备份。开启自动发现后可能重新加入。',
    };
    function skipReason(input) {
      const d = input.dataset;
      const paused = d.repoEnabled !== '1' || d.repoStatus === 'paused';
      if (['resume', 'sync'].includes(action.value) && d.repoMode === 'protected') return '独立维护，跳过';
      if (action.value === 'pause' && d.repoEnabled === '0' && d.repoStatus === 'paused' && d.repoQueued !== '1') return '已暂停，跳过';
      if (action.value === 'resume' && !paused) return '已启用，跳过';
      if (action.value === 'sync' && paused) return '需先恢复，跳过';
      if (action.value === 'sync' && d.repoQueued === '1') return '已在队列，跳过';
      if (action.value === 'protect' && d.repoMode === 'protected' && d.repoEnabled === '0' && d.repoStatus === 'paused') return '已独立维护，跳过';
      return '';
    }
    updateSelection = () => {
      const all = checks(), available = all.filter(input => !input.disabled), chosen = selected();
      all.forEach(input => input.closest('tr')?.classList.toggle('is-selected', input.checked && !input.disabled));
      selectAll.checked = available.length > 0 && chosen.length === available.length;
      selectAll.indeterminate = chosen.length > 0 && chosen.length < available.length;
      selectAll.disabled = !available.length;
      clear.disabled = !chosen.length;
      count.textContent = chosen.length ? `已选择 ${chosen.length} 个仓库` : '未选择仓库';
      actionButtons.forEach(button => { button.disabled = !chosen.length || chosen.length > 500; });
      help.textContent = chosen.length ? '选择期间暂停列表自动刷新；提交时会重新检查状态。每次最多 500 个仓库。' :
        '选中仓库后点击操作，预览确认后执行；每次最多 500 个。';
      // Changing the operation alone must not freeze refresh after selection is cleared.
      delete bulkForm.dataset.editing;
    };
    selectAll.addEventListener('change', () => {
      let n = 0;
      checks().forEach(input => { input.checked = !input.disabled && selectAll.checked && n++ < 500; });
      updateSelection();
    });
    clear.addEventListener('click', () => {
      checks().forEach(input => { input.checked = false; });
      updateSelection();
    });
    document.addEventListener('change', event => {
      if (event.target.matches?.('[data-repo-select]')) {
        if (selected().length > 500) {
          event.target.checked = false;
          updateSelection();
          help.textContent = '每次最多选择 500 个仓库，请分批操作。';
        } else updateSelection();
      }
    });
    let approved = false;
    bulkForm.addEventListener('submit', event => {
      preview = event.submitter;
      action.value = preview?.dataset.bulkAction || '';
      if (bulkForm.dataset.submitting || !selected().length || !labels[action.value] || selected().length > 500) {
        event.preventDefault();
        return;
      }
      if (approved) { approved = false; return; }
      const chosen = selected();
      const eligible = chosen.filter(input => !skipReason(input)).length;
      const summary = `选择 ${chosen.length} 个，将处理 ${eligible} 个，跳过 ${chosen.length - eligible} 个。`;
      if (!bulkDialog || typeof bulkDialog.showModal !== 'function') {
        if (!eligible || !window.confirm(`${labels[action.value]}：${summary}\n${explanations[action.value]}`)) event.preventDefault();
        return;
      }
      event.preventDefault();
      bulkDialog.querySelector('#bulk-dialog-title').textContent = '批量' + labels[action.value];
      bulkDialog.querySelector('#bulk-dialog-summary').textContent = summary;
      bulkDialog.querySelector('#bulk-dialog-help').textContent = explanations[action.value];
      const list = bulkDialog.querySelector('#bulk-dialog-repos');
      list.replaceChildren();
      chosen.forEach(input => {
        const item = document.createElement('li');
        const name = document.createElement('span');
        name.textContent = input.dataset.repoName + (input.dataset.repoMode === 'force' ? '（强制同步）' : '');
        const outcome = document.createElement('span');
        const reason = skipReason(input);
        outcome.textContent = reason || '将' + labels[action.value];
        outcome.className = reason ? 'muted' : 'bulk-will-change';
        item.append(name, outcome);
        list.append(item);
      });
      confirm.textContent = eligible ? `确认${labels[action.value]} ${eligible} 个` : '没有可操作的仓库';
      confirm.disabled = !eligible;
      confirm.className = action.value === 'remove' ? 'danger' : 'primary';
      bulkDialog.showModal();
    });
    if (bulkDialog && confirm) {
      bulkDialog.querySelector('[data-bulk-cancel]').addEventListener('click', () => bulkDialog.close());
      confirm.addEventListener('click', () => {
        bulkDialog.close();
        approved = true;
        bulkForm.requestSubmit(preview);
      });
      bulkDialog.addEventListener('close', () => preview?.focus());
    }
    updateSelection();
  }

  const boundForms = new WeakSet();
  function bindForms(container) {
    container.querySelectorAll('form').forEach(form => {
      if (boundForms.has(form)) return;
      boundForms.add(form);
      form.addEventListener('input', () => { form.dataset.editing = 'true'; });
      form.addEventListener('submit', event => {
        if (event.defaultPrevented) return;
        if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) {
          event.preventDefault();
          return;
        }
        const button = event.submitter;
        if (button) {
          // Preserve submitter values; disable only after native form data is captured.
          setTimeout(() => { button.disabled = true; button.setAttribute('aria-busy', 'true'); }, 0);
        }
        form.dataset.submitting = 'true';
        delete form.dataset.editing;
      });
      form.addEventListener('reset', () => { delete form.dataset.editing; });
    });
  }
  bindForms(document);
  // Background refresh replaces only the list region. A full reload re-fetched
  // the shell, CSS and JS every 30 seconds and made the panel feel sluggish, so
  // the server renders the same rows into a fragment that is swapped in place.
  const refresh = document.querySelector('[data-auto-refresh]');
  if (refresh) {
    const region = document.querySelector('#' + refresh.dataset.refreshRegion);
    // Keep reading and keyboard focus intact, including while a fetch is in flight.
    const busy = () => document.hidden || document.body.classList.contains('nav-open') ||
      errorDialog?.open || bulkDialog?.open || document.querySelector('[data-repo-select]:checked') || region?.querySelector('details[open]') ||
      document.querySelector('form[data-editing], form[data-submitting]') ||
      document.activeElement?.matches('input, select, textarea, button') ||
      (region?.contains(document.activeElement) && document.activeElement?.matches('a[href], summary'));
    let inFlight = false;
    // Cloudflare rewrites the HTML it proxies and drops the ETag header, so the
    // revision the server stamps into the markup is sent back as a query
    // parameter instead of an If-None-Match header.
    const revision = () => region?.querySelector('[data-revision]')?.dataset.revision || '';
    const update = async () => {
      if (busy() || inFlight) return;
      const url = region && region.dataset.refreshUrl;
      if (!url || typeof window.fetch !== 'function') { location.reload(); return; }
      inFlight = true;
      try {
        const known = revision();
        const target = known ? url + (url.includes('?') ? '&' : '?') + 'v=' + known : url;
        const response = await window.fetch(target, { cache: 'no-store' });
        // An expired session must fall back to a navigation so /login is shown.
        if (response.status === 401) { location.reload(); return; }
        // 204: nothing changed since the last tick, so the DOM stays untouched.
        if (response.status === 204 || !response.ok) return;
        const html = await response.text();
        if (!busy() && region.innerHTML !== html) {
          region.innerHTML = html;
          localizeTimes(region);
          bindForms(region);
          updateSelection();
        }
      } catch { /* Keep the current view; the next tick retries. */ }
      finally { inFlight = false; }
    };
    setInterval(update, Number(refresh.dataset.autoRefresh) * 1000);
    if (document.addEventListener) {
      document.addEventListener('visibilitychange', () => { if (!document.hidden) update(); });
    }
  }
  window.addEventListener('pageshow', () => {
    document.querySelectorAll('form').forEach(form => { delete form.dataset.submitting; });
    document.querySelectorAll('button[aria-busy]').forEach(button => {
      button.disabled = false;
      button.removeAttribute('aria-busy');
    });
    updateSelection();
  });
})();
