/* ─────────────────────────────────────────────────────────────────────
   main.js — Global utilities: toast, CSRF, fetch helpers, sidebar
   ───────────────────────────────────────────────────────────────────── */

// ── CSRF Token ─────────────────────────────────────────────────────────────
function getCsrfToken() {
  return document.cookie
    .split('; ')
    .find(r => r.startsWith('csrf_token='))
    ?.split('=')[1] || '';
}

// ── Authenticated fetch with CSRF header ────────────────────────────────────
async function apiFetch(url, options = {}) {
  const headers = {
    'X-CSRF-Token': getCsrfToken(),
    ...(options.headers || {}),
  };
  const resp = await fetch(url, { ...options, headers });
  if (!resp.ok) {
    const text = await resp.text();
    let parsedData = null;
    let errMessage = text || `HTTP ${resp.status}`;
    try {
      parsedData = JSON.parse(text);
      if (parsedData && parsedData.detail) {
        if (typeof parsedData.detail === 'object') {
          errMessage = parsedData.detail.message || JSON.stringify(parsedData.detail);
        } else {
          errMessage = parsedData.detail;
        }
      }
    } catch (_) {}
    const err = new Error(errMessage);
    err.status = resp.status;
    err.data = parsedData;
    err.rawText = text;
    throw err;
  }
  return resp;
}

async function apiJSON(url, options = {}) {
  const resp = await apiFetch(url, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
  });
  return resp.json();
}

// ── Toast Notifications ─────────────────────────────────────────────────────
function showToast(message, type = 'info', duration = 4000) {
  let container = document.getElementById('toast-container');
  if (!container) {
    container = document.createElement('div');
    container.id = 'toast-container';
    document.body.appendChild(container);
  }

  // Clean raw YouTube upload limit errors if passed directly
  let cleanMsg = String(message || '');
  if (
    cleanMsg.includes('uploadLimitExceeded') ||
    cleanMsg.includes('exceeded the number of videos') ||
    cleanMsg.includes('Daily Upload Limit Exceeded')
  ) {
    cleanMsg = '⚠️ YouTube Daily Upload Limit Reached: Unlock Advanced Features or switch channels.';
    if (type === 'error') type = 'warn';
    duration = Math.max(duration, 6000);
  }

  const icons = { success: '✅', error: '❌', info: 'ℹ️', warn: '⚠️' };
  const toast = document.createElement('div');
  toast.className = `toast ${type}`;
  toast.innerHTML = `<span>${icons[type] || 'ℹ️'}</span><span>${cleanMsg}</span>`;
  container.appendChild(toast);

  setTimeout(() => {
    toast.style.animation = 'fadeOut 0.3s ease forwards';
    setTimeout(() => toast.remove(), 300);
  }, duration);
}

// ── Sidebar toggle (mobile) ─────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  const menuBtn = document.getElementById('menu-btn');
  const sidebar = document.querySelector('.sidebar');

  menuBtn?.addEventListener('click', () => {
    sidebar?.classList.toggle('open');
  });

  // Close sidebar when clicking outside on mobile
  document.addEventListener('click', (e) => {
    if (window.innerWidth <= 768 && sidebar?.classList.contains('open')) {
      if (!sidebar.contains(e.target) && e.target !== menuBtn) {
        sidebar.classList.remove('open');
      }
    }
  });

  // Active nav highlighting
  const currentPath = window.location.pathname;
  document.querySelectorAll('.nav-item').forEach(item => {
    const href = item.getAttribute('href') || '';
    if (href && currentPath.startsWith(href) && href !== '/') {
      item.classList.add('active');
    } else if (href === '/' && currentPath === '/') {
      item.classList.add('active');
    }
  });
});

// ── Countdown & Time Remaining Methods ──────────────────────────────────────
function parseTargetDate(target) {
  if (!target) return null;
  if (target instanceof Date) return target;
  let str = String(target).trim();
  // Normalize date string with T
  if (!str.includes('T') && str.includes(' ')) {
    str = str.replace(' ', 'T');
  }
  // Ensure UTC timezone if none provided
  if (!str.endsWith('Z') && !/[+-]\d{2}(:?\d{2})?$/.test(str)) {
    str = str + 'Z';
  }
  const d = new Date(str);
  return isNaN(d.getTime()) ? new Date(target) : d;
}

function getTimeRemaining(targetISOString) {
  const target = parseTargetDate(targetISOString);
  if (!target || isNaN(target.getTime())) {
    return {
      totalSeconds: 0,
      days: 0,
      hours: 0,
      minutes: 0,
      seconds: 0,
      isDue: false,
      formatted: '—',
      formattedShort: '--:--:--',
      formattedLong: 'Not scheduled',
    };
  }

  const now = Date.now();
  const diffMs = target.getTime() - now;
  const totalSeconds = Math.floor(diffMs / 1000);

  if (totalSeconds <= 0) {
    return {
      totalSeconds,
      days: 0,
      hours: 0,
      minutes: 0,
      seconds: 0,
      isDue: true,
      formatted: 'Due now',
      formattedShort: '00:00:00',
      formattedLong: 'Due now / Ready to upload',
    };
  }

  const days = Math.floor(totalSeconds / 86400);
  const remSec = totalSeconds % 86400;
  const hours = Math.floor(remSec / 3600);
  const minutes = Math.floor((remSec % 3600) / 60);
  const seconds = remSec % 60;

  const parts = [];
  if (days > 0) parts.push(`${days}d`);
  if (hours > 0 || days > 0) parts.push(`${hours}h`);
  if (minutes > 0 || hours > 0 || days > 0) parts.push(`${minutes}m`);
  parts.push(`${seconds}s`);
  const formatted = parts.join(' ');

  const formattedShort = (days > 0 ? `${days}d ` : '') +
    `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`;

  const longParts = [];
  if (days > 0) longParts.push(`${days} day${days > 1 ? 's' : ''}`);
  if (hours > 0) longParts.push(`${hours} hr${hours > 1 ? 's' : ''}`);
  if (minutes > 0) longParts.push(`${minutes} min${minutes > 1 ? 's' : ''}`);
  if (seconds > 0 && days === 0) longParts.push(`${seconds} sec${seconds > 1 ? 's' : ''}`);
  const formattedLong = longParts.length > 0 ? longParts.join(', ') : 'Less than a second';

  return {
    totalSeconds,
    days,
    hours,
    minutes,
    seconds,
    isDue: false,
    formatted,
    formattedShort,
    formattedLong,
  };
}

function formatTimeRemaining(targetISOString) {
  return getTimeRemaining(targetISOString).formatted;
}

function initCountdown(targetISOString, containerId) {
  const container = document.getElementById(containerId);
  if (!container) return;

  const daysEl = container.querySelector('[data-unit="days"]');
  const hoursEl = container.querySelector('[data-unit="hours"]');
  const minsEl  = container.querySelector('[data-unit="mins"]');
  const secsEl  = container.querySelector('[data-unit="secs"]');

  function update() {
    const rem = getTimeRemaining(targetISOString);
    if (rem.isDue) {
      if (daysEl)  daysEl.textContent  = '00';
      if (hoursEl) hoursEl.textContent = '00';
      if (minsEl)  minsEl.textContent  = '00';
      if (secsEl)  secsEl.textContent  = '00';
      return;
    }

    if (daysEl)  daysEl.textContent  = String(rem.days).padStart(2, '0');
    if (hoursEl) hoursEl.textContent = String(rem.hours).padStart(2, '0');
    if (minsEl)  minsEl.textContent  = String(rem.minutes).padStart(2, '0');
    if (secsEl)  secsEl.textContent  = String(rem.seconds).padStart(2, '0');
  }

  update();
  return setInterval(update, 1000);
}

// ── Confirm Dialog ──────────────────────────────────────────────────────────
function confirmAction(message, callback) {
  if (window.confirm(message)) callback();
}

// ── Delete with confirmation ────────────────────────────────────────────────
async function deleteResource(url, onSuccess) {
  if (!confirm('Are you sure you want to delete this? This cannot be undone.')) return;
  try {
    await apiFetch(url, { method: 'DELETE' });
    showToast('Deleted successfully', 'success');
    if (onSuccess) onSuccess();
    else location.reload();
  } catch (err) {
    showToast(`Delete failed: ${err.message}`, 'error');
  }
}

// ── Error Parsing & Resolution Card Rendering ──────────────────────────────
function parseApiError(err) {
  let raw = '';
  let detailObj = null;
  if (err && err.data && err.data.detail && typeof err.data.detail === 'object') {
    detailObj = err.data.detail;
  }
  if (err && err.rawText) {
    raw = err.rawText;
  } else if (err && err.message) {
    raw = err.message;
  } else {
    raw = String(err || '');
  }

  const isUploadLimit = 
    (detailObj && detailObj.error_type === 'upload_limit_exceeded') ||
    raw.includes('uploadLimitExceeded') || 
    raw.includes('exceeded the number of videos') ||
    raw.includes('Daily Upload Limit Exceeded');

  const isQuotaExceeded =
    (detailObj && detailObj.error_type === 'quota_exceeded') ||
    raw.includes('quotaExceeded') ||
    raw.includes('Quota Exceeded');

  let title = 'Upload Failed';
  let message = err?.message || raw || 'An unknown error occurred';
  if (isUploadLimit) {
    title = 'YouTube Daily Upload Limit Exceeded';
    message = detailObj?.message || 'This YouTube channel has reached Google\'s maximum video uploads for today (24-hour limit).';
  } else if (isQuotaExceeded) {
    title = 'YouTube API Quota Exceeded';
    message = detailObj?.message || 'Daily Google API quota exhausted (10,000 units/day project cap). Resets at midnight PT.';
  }

  return {
    isUploadLimit,
    isQuotaExceeded,
    title,
    message,
    resolution: detailObj?.resolution || null,
    studioUrl: detailObj?.studio_url || 'https://studio.youtube.com/',
    raw,
  };
}

function renderUploadLimitCard(err) {
  const info = parseApiError(err);
  if (info.isUploadLimit) {
    return `
      <div style="background:linear-gradient(135deg, rgba(239,68,68,0.18), rgba(245,158,11,0.12)); border:1px solid rgba(239,68,68,0.45); border-radius:10px; padding:16px; margin-bottom:12px; color:#f8fafc; text-align:left;">
        <div style="display:flex; align-items:center; gap:8px; font-weight:800; font-size:14.5px; color:#fca5a5; margin-bottom:8px;">
          <span>🛑 YouTube Daily Upload Limit Exceeded (Google 24h Cap)</span>
        </div>
        <p style="font-size:12.5px; color:#cbd5e1; line-height:1.5; margin:0 0 12px 0;">
          YouTube enforces a strict daily upload limit per channel (~5–15 uploads/24h for unverified/standard channels). This channel has hit Google's daily quota cap.
        </p>
        <div style="font-size:11.5px; font-weight:700; color:#fbbf24; text-transform:uppercase; letter-spacing:0.06em; margin-bottom:8px;">
          💡 How to resolve this in hosting:
        </div>
        <div style="display:flex; flex-direction:column; gap:8px; font-size:12px; color:#e2e8f0; line-height:1.45; margin-bottom:14px;">
          <div style="display:flex; align-items:flex-start; gap:8px; background:rgba(0,0,0,0.35); padding:9px 12px; border-radius:6px; border:1px solid rgba(255,255,255,0.06);">
            <span style="font-size:14px;">1️⃣</span>
            <div>
              <b style="color:#60a5fa;">Unlock Advanced Features (Instant Fix):</b> In YouTube Studio &rarr; <b>Settings &rarr; Channel &rarr; Feature eligibility</b>, enable <b>Advanced features</b> (via 30-sec Video Verification or ID) to unlock 100+ daily uploads permanently.
            </div>
          </div>
          <div style="display:flex; align-items:flex-start; gap:8px; background:rgba(0,0,0,0.35); padding:9px 12px; border-radius:6px; border:1px solid rgba(255,255,255,0.06);">
            <span style="font-size:14px;">2️⃣</span>
            <div>
              <b style="color:#34d399;">Switch YouTube Channel:</b> Upload limits are strictly per channel. Switch to another connected channel in your <b>Channels</b> tab to continue uploading right now.
            </div>
          </div>
          <div style="display:flex; align-items:flex-start; gap:8px; background:rgba(0,0,0,0.35); padding:9px 12px; border-radius:6px; border:1px solid rgba(255,255,255,0.06);">
            <span style="font-size:14px;">3️⃣</span>
            <div>
              <b style="color:#fbbf24;">Wait 24 Hours:</b> Google operates on a rolling 24-hour window from each video's upload timestamp. Limit slots will reopen automatically.
            </div>
          </div>
        </div>
        <div style="display:flex; gap:10px; flex-wrap:wrap; align-items:center;">
          <a href="${info.studioUrl}" target="_blank" rel="noopener noreferrer" style="display:inline-flex; align-items:center; gap:6px; font-weight:700; font-size:12px; padding:8px 14px; background:linear-gradient(135deg, #3b82f6, #2563eb); text-decoration:none; color:#fff; border-radius:6px;">
            🔗 Open YouTube Studio Settings
          </a>
          <a href="/api/channels" style="display:inline-flex; align-items:center; gap:6px; font-size:12px; padding:8px 12px; border:1px solid rgba(255,255,255,0.2); color:#cbd5e1; text-decoration:none; border-radius:6px; background:rgba(255,255,255,0.06);">
            📺 Switch YouTube Channel
          </a>
        </div>
      </div>
    `;
  }
  if (info.isQuotaExceeded) {
    return `
      <div style="background:linear-gradient(135deg, rgba(239,68,68,0.18), rgba(245,158,11,0.12)); border:1px solid rgba(239,68,68,0.45); border-radius:10px; padding:16px; margin-bottom:12px; color:#f8fafc; text-align:left;">
        <div style="font-weight:800; font-size:14.5px; color:#fca5a5; margin-bottom:8px;">
          🛑 YouTube API Quota Exceeded (10,000 Units/Day Cap)
        </div>
        <p style="font-size:12.5px; color:#cbd5e1; line-height:1.5; margin:0 0 10px 0;">
          Your Google Cloud Project has consumed all 10,000 daily API quota units.
        </p>
        <div style="font-size:12px; color:#fbbf24; background:rgba(0,0,0,0.35); padding:8px 10px; border-radius:6px; border:1px solid rgba(255,255,255,0.06);">
          ⏳ Google resets the daily quota automatically at <b>midnight Pacific Time (PT)</b>.
        </div>
      </div>
    `;
  }
  return `
    <div style="background:rgba(239,68,68,0.12); border:1px solid rgba(239,68,68,0.4); border-radius:8px; padding:12px; text-align:left;">
      <div style="font-weight:700; font-size:13.5px; margin-bottom:4px; color:#fca5a5;">❌ ${info.title}</div>
      <div style="font-size:12px; line-height:1.45; color:#fecaca;">${info.message}</div>
    </div>
  `;
}

// ── Expose globals ──────────────────────────────────────────────────────────
window.YTBot = {
  apiFetch,
  apiJSON,
  showToast,
  parseApiError,
  renderUploadLimitCard,
  initCountdown,
  getTimeRemaining,
  formatTimeRemaining,
  parseTargetDate,
  deleteResource,
  getCsrfToken
};
