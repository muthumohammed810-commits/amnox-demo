/* Public demo guardrails: the server also rejects these operations. */
(async function enableDemoModeWhenConfigured() {
  try {
    const response = await fetch('/api/health', { credentials: 'same-origin' });
    if (!response.ok || (await response.json()).mode !== 'DEMO ONLY') return;
    state.demoMode = true;
    const label = document.querySelector('#environment-label');
    const footer = document.querySelector('#footer-environment-label');
    if (label) label.textContent = 'DEMO ONLY · No real orders, sign-ups, or shop applications';
    if (footer) footer.textContent = '© 2026 AMNOX · DEMO ONLY · Synthetic catalog; no real orders accepted';

    renderCart = async function () {
      updateCount();
      const root = document.querySelector('#cart-content');
      root.innerHTML = '<div class="notice-box"><b>DEMO ONLY</b><br>Checkout is disabled. No delivery details or orders are collected.</div><a href="/products" data-link class="text-link">Browse the demo catalog ↗</a>';
      bindLinks();
    };
    applicationPage = function () {
      document.querySelector('#app').innerHTML = '<section class="auth-card"><div class="kicker">DEMO ONLY</div><h1>Shop applications are disabled.</h1><p>This preview does not collect owner contact details or applications.</p><a class="button button-green" href="/" data-link>Return to the market</a></section>';
    };
    const normalAuthPage = authPage;
    authPage = function (kind) {
      if (kind === 'customer' && location.pathname === '/account/register') {
        document.querySelector('#app').innerHTML = '<section class="auth-card"><div class="kicker">DEMO ONLY</div><h1>Account sign-up is disabled.</h1><p>This preview does not collect customer account details.</p><a class="button button-green" href="/" data-link>Browse the demo market</a></section>';
        return;
      }
      return normalAuthPage(kind);
    };
    const normalHome = home;
    home = async function () {
      await normalHome();
      document.querySelector('.banner > a')?.remove();
      const banner = document.querySelector('.banner');
      if (banner) banner.querySelector('p').textContent = 'DEMO ONLY · Shop applications are disabled.';
      document.querySelector('footer a[href="/shop/register"]')?.remove();
    };
    await render();
  } catch (_) {
    // Keep the ordinary local development experience if the health endpoint is unavailable.
  }
})();
