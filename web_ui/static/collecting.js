/* Advance only the explicit CSRF form; a GET never starts collection work. */
(() => {
  const form = document.getElementById('collection-step');
  if (!form) return;
  const button = form.querySelector('button[type="submit"]');
  const status = document.getElementById('collection-driver-status');
  let busy = false;
  let timer = null;
  let retries = 0;
  const maxRetries = 5;

  function stop() {
    busy = false;
    button.disabled = false;
    status.textContent = '自動収集を続けられませんでした。進捗ページを開き直し、状態を確認してください。';
  }

  async function advance() {
    if (busy) return;
    busy = true;
    button.disabled = true;
    status.textContent = '収集を進めています。';
    try {
      const response = await fetch(form.action, {
        method: 'POST', body: new FormData(form), credentials: 'same-origin',
        redirect: 'follow',
      });
      if (response.status === 429 || response.status === 503) {
        if (retries >= maxRetries) { stop(); return; }
        retries += 1;
        const requested = Number(response.headers.get('Retry-After'));
        const seconds = Number.isFinite(requested) && requested > 0
          ? Math.min(300, Math.max(1, requested)) : 60;
        status.textContent = `一時的に待機しています。${seconds}秒後に続きを確認します。`;
        busy = false;
        timer = setTimeout(() => { timer = null; advance(); }, seconds * 1000);
        return;
      }
      if (response.ok && response.redirected) {
        const target = new URL(response.url, window.location.href);
        if (target.origin === window.location.origin) {
          window.location.assign(target.href);
          return;
        }
      }
      stop();
    } catch (_) {
      // A lost response does not prove the POST was not executed. Ask for
      // a read-only progress reload, rather than blindly spending quota again.
      stop();
    }
  }

  form.addEventListener('submit', event => {
    event.preventDefault();
    if (busy) return;
    if (timer !== null) { clearTimeout(timer); timer = null; }
    retries = 0;
    advance();
  });
  advance();
})();
