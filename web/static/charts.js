/* ======================================================================
   Expense Tracker — Lightweight SVG Charts (no dependencies)
   ====================================================================== */

/**
 * Render a donut chart from category breakdown data.
 *
 * @param {string} containerId  - DOM id of the wrapper element.
 * @param {Array}  data         - [{category, total, percentage}, ...]
 */
function renderDonutChart(containerId, data) {
    const container = document.getElementById(containerId);
    if (!container || !data || data.length === 0) {
        if (container) container.innerHTML = '<div class="empty-state"><p>No expense data</p></div>';
        return;
    }

    const COLORS = [
        '#818cf8', '#a78bfa', '#c084fc', '#e879f9', '#f472b6',
        '#fb923c', '#fbbf24', '#34d399', '#60a5fa', '#f87171',
        '#2dd4bf', '#a3e635', '#38bdf8', '#fb7185', '#c4b5fd',
    ];

    const size = 180;
    const cx = size / 2, cy = size / 2;
    const outerR = 80, innerR = 52;

    let cumulative = 0;
    const slices = data.map((d, i) => {
        const startAngle = cumulative * 3.6;  // 360 / 100
        cumulative += d.percentage;
        const endAngle = cumulative * 3.6;
        return { ...d, startAngle, endAngle, color: COLORS[i % COLORS.length] };
    });

    function polarToCartesian(angle) {
        const rad = (angle - 90) * Math.PI / 180;
        return { x: cx + outerR * Math.cos(rad), y: cy + outerR * Math.sin(rad) };
    }
    function polarToCartesianInner(angle) {
        const rad = (angle - 90) * Math.PI / 180;
        return { x: cx + innerR * Math.cos(rad), y: cy + innerR * Math.sin(rad) };
    }

    let paths = '';
    slices.forEach(s => {
        if (s.percentage <= 0) return;
        const spanAngle = s.endAngle - s.startAngle;
        const largeArc = spanAngle > 180 ? 1 : 0;

        // If nearly full circle, render as two arcs
        if (s.percentage >= 99.9) {
            paths += `<circle cx="${cx}" cy="${cy}" r="${outerR}" fill="${s.color}" />`;
            paths += `<circle cx="${cx}" cy="${cy}" r="${innerR}" fill="var(--bg-card-solid)" />`;
            return;
        }

        const os = polarToCartesian(s.startAngle);
        const oe = polarToCartesian(s.endAngle);
        const is_ = polarToCartesianInner(s.endAngle);
        const ie = polarToCartesianInner(s.startAngle);

        paths += `<path d="
            M ${os.x} ${os.y}
            A ${outerR} ${outerR} 0 ${largeArc} 1 ${oe.x} ${oe.y}
            L ${is_.x} ${is_.y}
            A ${innerR} ${innerR} 0 ${largeArc} 0 ${ie.x} ${ie.y}
            Z
        " fill="${s.color}" opacity="0.85" stroke="var(--bg-card-solid)" stroke-width="1.5">
            <animate attributeName="opacity" from="0" to="0.85" dur="0.5s" fill="freeze" />
        </path>`;
    });

    // Center label
    const grandTotal = data.reduce((s, d) => s + d.total, 0);
    paths += `<text x="${cx}" y="${cy - 6}" text-anchor="middle" fill="var(--text-muted)" font-size="11" font-family="Inter">Total</text>`;
    paths += `<text x="${cx}" y="${cy + 14}" text-anchor="middle" fill="var(--text-primary)" font-size="16" font-weight="700" font-family="Inter">${formatCurrency(grandTotal)}</text>`;

    const svg = `<svg class="donut-svg" viewBox="0 0 ${size} ${size}" xmlns="http://www.w3.org/2000/svg">${paths}</svg>`;

    const legend = slices.map(s => `
        <div class="legend-item">
            <span class="legend-color" style="background:${s.color}"></span>
            <span class="legend-label">${s.category}</span>
            <span class="legend-value">${s.percentage.toFixed(1)}%</span>
        </div>
    `).join('');

    container.innerHTML = `
        <div class="donut-chart-wrapper">
            ${svg}
            <div class="donut-legend">${legend}</div>
        </div>
    `;
}


/**
 * Render a horizontal bar chart for monthly trend data.
 *
 * @param {string} containerId
 * @param {Array}  data  - [{month_label, income, expense}, ...]
 */
function renderTrendChart(containerId, data) {
    const container = document.getElementById(containerId);
    if (!container || !data || data.length === 0) {
        if (container) container.innerHTML = '<div class="empty-state"><p>No trend data</p></div>';
        return;
    }

    const maxVal = Math.max(...data.map(d => Math.max(d.income, d.expense)), 1);

    const rows = data.map(d => {
        const incPct = (d.income / maxVal * 100).toFixed(1);
        const expPct = (d.expense / maxVal * 100).toFixed(1);
        const label = d.month_label;

        return `
            <div style="margin-bottom: 0.75rem;">
                <div style="display:flex; align-items:center; gap:0.5rem; margin-bottom:0.25rem;">
                    <span class="bar-label" style="min-width:60px">${label}</span>
                    <div class="bar-track" style="flex:1">
                        <div class="bar-fill" style="width:${incPct}%; background: var(--income); opacity:0.7;"></div>
                    </div>
                    <span class="bar-value" style="color:var(--income); min-width:80px;">${formatCurrency(d.income)}</span>
                </div>
                <div style="display:flex; align-items:center; gap:0.5rem;">
                    <span class="bar-label" style="min-width:60px"></span>
                    <div class="bar-track" style="flex:1">
                        <div class="bar-fill" style="width:${expPct}%; background: var(--expense); opacity:0.7;"></div>
                    </div>
                    <span class="bar-value" style="color:var(--expense); min-width:80px;">${formatCurrency(d.expense)}</span>
                </div>
            </div>
        `;
    }).join('');

    container.innerHTML = `
        <div style="display:flex; gap:1.5rem; margin-bottom:0.75rem; font-size:0.78rem; color:var(--text-muted);">
            <span>🟢 Income</span>
            <span>🔴 Expense</span>
        </div>
        ${rows}
    `;
}


/**
 * Format a number as a currency string.
 */
function formatCurrency(value) {
    return new Intl.NumberFormat('en-IN', {
        style: 'currency',
        currency: 'INR',
        minimumFractionDigits: 0,
        maximumFractionDigits: 0,
    }).format(value);
}


/* --- Upload drag-and-drop enhancement --- */
document.addEventListener('DOMContentLoaded', () => {
    const dropZone = document.querySelector('.upload-zone');
    if (dropZone) {
        ['dragenter', 'dragover'].forEach(ev => {
            dropZone.addEventListener(ev, (e) => {
                e.preventDefault();
                dropZone.classList.add('drag-over');
            });
        });
        ['dragleave', 'drop'].forEach(ev => {
            dropZone.addEventListener(ev, () => {
                dropZone.classList.remove('drag-over');
            });
        });

        // Show selected file name
        const fileInput = dropZone.querySelector('input[type="file"]');
        if (fileInput) {
            fileInput.addEventListener('change', () => {
                const nameEl = dropZone.querySelector('.selected-file-name');
                if (fileInput.files.length > 0) {
                    const name = fileInput.files[0].name;
                    if (nameEl) {
                        nameEl.textContent = name;
                    } else {
                        const span = document.createElement('p');
                        span.className = 'selected-file-name';
                        span.style.color = 'var(--accent-bright)';
                        span.style.marginTop = '0.5rem';
                        span.style.fontWeight = '500';
                        span.textContent = name;
                        dropZone.appendChild(span);
                    }
                }
            });
        }
    }
});
