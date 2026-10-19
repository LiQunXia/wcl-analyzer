/* Shared language preference, API requests, and Wowhead localization. */
(() => {
  const normalize = (value) => /^(en|en-us|en-gb|enus|engb)$/i.test(String(value || '').replaceAll('_', '-')) ? 'en' : 'zh-CN';
  let saved;
  try { saved = localStorage.getItem('wcl.language'); } catch (_) { /* private browsing */ }
  const requested = new URLSearchParams(location.search).get('lang');
  const lang = normalize(requested || saved || 'zh-CN');
  try { localStorage.setItem('wcl.language', lang); } catch (_) { /* preference is optional */ }
  document.documentElement.lang = lang;
  const t = (zh, en) => lang === 'en' ? en : zh;

  function apiUrl(path) {
    const url = new URL(path, location.href);
    url.searchParams.set('lang', lang);
    return url.pathname + url.search + url.hash;
  }

  function spellAttrs(id) {
    const spell = Number(id);
    if (!Number.isSafeInteger(spell) || spell <= 0) return '';
    const domain = lang === 'en' ? 'www' : 'cn';
    return `href="https://www.wowhead.com/${domain === 'cn' ? 'cn/' : ''}spell=${spell}" data-wowhead="spell=${spell}&amp;domain=${domain}" target="_blank" rel="noopener noreferrer"`;
  }

  function mountLanguageSwitcher(container) {
    if (typeof container === 'string') container = document.querySelector(container);
    if (!container) return;
    const label = document.createElement('label');
    label.className = 'language-switcher';
    label.style.cssText = 'display:inline-flex;align-items:center;gap:6px;font-size:12px;white-space:nowrap;';
    const caption = document.createElement('span');
    caption.textContent = t('语言', 'Language');
    const select = document.createElement('select');
    select.setAttribute('aria-label', t('界面与日志语言', 'Interface and combat log language'));
    select.style.cssText = 'font:inherit;color:inherit;background:#20232b;border:1px solid #454956;border-radius:6px;padding:5px 8px;cursor:pointer;';
    select.add(new Option('中文', 'zh-CN'));
    select.add(new Option('English', 'en'));
    select.value = lang;
    select.addEventListener('change', () => {
      const next = normalize(select.value);
      try { localStorage.setItem('wcl.language', next); } catch (_) { /* URL still carries language */ }
      window.dispatchEvent(new CustomEvent('wcl-language-change', {detail: {lang: next}}));
      const url = new URL(location.href);
      url.searchParams.set('lang', next);
      location.assign(url.href);
    });
    label.append(caption, select);
    container.append(label);
  }

  window.I18N = Object.freeze({lang, t, apiUrl, spellAttrs, mountLanguageSwitcher});
})();
