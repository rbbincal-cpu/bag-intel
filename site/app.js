/* Shared helpers for all dashboard pages */
const GOLD = '#B08D3E';
const PALETTE = ['#B08D3E', '#1C1917', '#9F1239', '#3F6212', '#1D4ED8', '#7C3AED'];

const peso = n => n == null ? '—' :
  '₱' + (n >= 1e6 ? (n / 1e6).toFixed(2) + 'M' : n >= 1e3 ? (n / 1e3).toFixed(0) + 'K' : Math.round(n));
const pesoFull = n => n == null ? '—' : '₱' + Math.round(n).toLocaleString();
const esc = s => (s || '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

async function loadData() {
  if (window.__TEST_DATA) return window.__TEST_DATA;
  const r = await fetch('data.json?v=' + Date.now());
  if (!r.ok) throw new Error('data.json missing — run the pipeline first');
  return r.json();
}

function setUpdated(data) {
  const el = document.querySelector('.updated');
  if (el) el.textContent = 'Updated ' + (data.generated_at || data.generated) + ' PHT · refreshes every 6h';
}

function siteColor(data, key) {
  const idx = data.sites.findIndex(s => s.key === key);
  return PALETTE[idx % PALETTE.length];
}

/* Build cumulative-by-month-to-date series from per-day rows */
function cumulMTD(rows, siteKey, monthPrefix, field) {
  const days = rows.filter(r => r.site === siteKey && r.d && r.d.startsWith(monthPrefix))
                   .sort((a, b) => a.d < b.d ? -1 : 1);
  let acc = 0;
  return days.map(r => ({ x: r.d, y: (acc += r[field]) }));
}

function lineChart(ctx, datasets, opts = {}) {
  return new Chart(ctx, {
    type: 'line',
    data: { datasets },
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { mode: 'nearest', intersect: false },
      scales: {
        x: { type: 'category', grid: { display: false }, ticks: { maxTicksLimit: 8, font: { size: 10 } } },
        y: { ticks: { callback: v => opts.money ? peso(v) : v, font: { size: 10 } }, grid: { color: '#F0EAE0' } }
      },
      plugins: { legend: { labels: { boxWidth: 10, font: { size: 11 } } },
        tooltip: { callbacks: { label: c => c.dataset.label + ': ' + (opts.money ? pesoFull(c.parsed.y) : c.parsed.y) } } }
    }
  });
}

function ds(label, points, color, fill = false) {
  return { label, data: points, borderColor: color, backgroundColor: color + '22',
           pointRadius: 1.5, borderWidth: 2, tension: .25, fill };
}
