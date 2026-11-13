/* Web interna de trading (/trading). JavaScript sin dependencias salvo Lightweight Charts. */
(function () {
    "use strict";

    const root = document.getElementById("td-app");
    if (!root) {
        return;
    }
    const USER = root.dataset.user || "";
    const CCY = root.dataset.currency || "EUR";
    const POLL_MS = 2000;
    const HISTORY_MS = 60000;
    const ODOO = "/odoo/action-trading_base.";

    const ASSET = {
        stock: ["Acciones", "#4f8cff"], etf: ["ETF", "#22d3ee"], crypto: ["Cripto", "#f59e0b"],
        forex: ["Divisas", "#a78bfa"], commodity: ["Materias primas", "#f472b6"],
        index: ["Índices", "#34d399"], other: ["Otros", "#94a3b8"],
    };
    const PAGES = [
        ["resumen", "Resumen", "M3 13h8V3H3v10zm0 8h8v-6H3v6zm10 0h8V11h-8v10zm0-18v6h8V3h-8z"],
        ["posiciones", "Posiciones", "M4 19h16M4 15l4-5 4 3 6-8"],
        ["operaciones", "Operaciones", "M7 7h13M7 7l3-3M7 7l3 3M17 17H4m13 0-3-3m3 3-3 3"],
        ["carteras", "Carteras", "M3 7h18v13H3zM8 7V4h8v3"],
        ["mercado", "Mercado", "M3 3v18h18M7 14l3-3 3 3 5-6"],
        ["renta", "Renta", "M9 14l6-6M9.5 8.5h.01M14.5 13.5h.01M5 3h14v18l-3-2-2 2-2-2-2 2-2-2-3 2z"],
    ];

    const state = {
        page: "resumen",
        summary: null,
        history: null,
        historyDays: 30,
        historyAt: 0,
        closed: null,
        closedYear: null,
        tax: null,
        taxYear: new Date().getFullYear(),
        search: "",
        posFilter: "all",
        sort: { key: "current_value", dir: -1 },
        prev: {},
        chart: null,
        series: null,
        error: "",
    };

    // ------------------------------------------------------------------
    // Formato
    // ------------------------------------------------------------------
    const nf = {};
    function money(value, ccy, digits) {
        const c = ccy || CCY;
        const key = c + (digits ?? 2);
        if (!nf[key]) {
            try {
                nf[key] = new Intl.NumberFormat("es-ES", {
                    style: "currency", currency: c,
                    minimumFractionDigits: digits ?? 2, maximumFractionDigits: digits ?? 2,
                });
            } catch (e) {
                nf[key] = new Intl.NumberFormat("es-ES", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
            }
        }
        return nf[key].format(value || 0);
    }
    function signed(value, ccy) {
        const v = value || 0;
        return (v > 0 ? "+" : v < 0 ? "−" : "") + money(Math.abs(v), ccy);
    }
    function num(value, digits) {
        return new Intl.NumberFormat("es-ES", {
            minimumFractionDigits: digits ?? 2, maximumFractionDigits: digits ?? 2,
        }).format(value || 0);
    }
    function price(value) {
        const v = Math.abs(value || 0);
        const d = v === 0 ? 2 : v < 2 ? 5 : v < 20 ? 4 : v < 1000 ? 2 : 2;
        return num(value, d);
    }
    function pct(value, withSign = true) {
        const v = value || 0;
        return (withSign && v > 0 ? "+" : v < 0 ? "−" : "") + num(Math.abs(v), 2) + " %";
    }
    function cls(value) {
        return value > 0 ? "up" : value < 0 ? "down" : "muted";
    }
    function parseDate(s) {
        return s ? new Date(s.replace(" ", "T") + "Z") : null;
    }
    function fdate(s, withTime) {
        const d = parseDate(s);
        if (!d) {
            return "—";
        }
        const opts = { day: "2-digit", month: "short" };
        if (d.getFullYear() !== new Date().getFullYear()) {
            opts.year = "numeric";
        }
        if (withTime) {
            opts.hour = "2-digit";
            opts.minute = "2-digit";
        }
        return d.toLocaleString("es-ES", opts).replace(".", "");
    }
    function esc(s) {
        return String(s ?? "").replace(/[&<>"']/g, (c) => ({
            "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
        })[c]);
    }
    function icon(path) {
        return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="${path}"/></svg>`;
    }
    function hue(text) {
        let h = 0;
        for (const ch of String(text)) {
            h = (h * 31 + ch.charCodeAt(0)) % 360;
        }
        return h;
    }
    function symBadge(symbol, assetType) {
        const s = String(symbol || "?").replace(/\..*$/, "");
        const label = s.length > 4 ? s.slice(0, 4) : s;
        const h = hue(s);
        const base = (ASSET[assetType] || ASSET.other)[1];
        return `<span class="td-sym" style="background: linear-gradient(135deg, hsl(${h} 55% 42%), ${base}99)">${esc(label)}</span>`;
    }
    function initials(name) {
        return String(name || "?").split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0].toUpperCase()).join("");
    }
    function odooLink(action, id) {
        return `${ODOO}${action}${id ? "/" + id : ""}`;
    }
    function openOdoo(action, id) {
        window.open(odooLink(action, id), "_blank", "noopener");
    }

    // ------------------------------------------------------------------
    // Datos
    // ------------------------------------------------------------------
    async function getJSON(url) {
        const res = await fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" } });
        if (res.status === 401 || res.redirected) {
            window.location.href = "/web/login?redirect=" + encodeURIComponent(location.pathname + location.hash);
            throw new Error("Sesión caducada");
        }
        if (!res.ok) {
            throw new Error(`Error ${res.status}`);
        }
        return res.json();
    }
    async function postRPC(url, params) {
        const res = await fetch(url, {
            method: "POST", credentials: "same-origin",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ jsonrpc: "2.0", method: "call", params: params || {} }),
        });
        const data = await res.json();
        if (data.error) {
            throw new Error(data.error.data?.message || data.error.message || "Error");
        }
        return data.result;
    }

    let toastTimer = null;
    function toast(message, isError) {
        let el = document.querySelector(".td-toast");
        if (!el) {
            el = document.createElement("div");
            el.className = "td-toast";
            document.body.appendChild(el);
        }
        el.textContent = message;
        el.classList.toggle("is-error", !!isError);
        requestAnimationFrame(() => el.classList.add("is-on"));
        clearTimeout(toastTimer);
        toastTimer = setTimeout(() => el.classList.remove("is-on"), 3800);
    }

    // ------------------------------------------------------------------
    // Estructura
    // ------------------------------------------------------------------
    function renderShell() {
        root.innerHTML = `
        <div class="td-shell">
            <aside class="td-side">
                <div class="td-brand">
                    <div class="td-brand-mark">${icon("M4 18l6-7 4 4 6-9")}</div>
                    <span>TRADING<small>Panel personal</small></span>
                </div>
                <nav class="td-nav">
                    ${PAGES.map(([key, label, path]) => `
                        <a href="#/${key}" data-page="${key}" title="${label}">${icon(path)}<span>${label}</span></a>`).join("")}
                </nav>
                <div class="td-side-foot">
                    <a href="${odooLink("action_trading_live_board")}" target="_blank" rel="noopener">${icon("M12 8v4l3 2M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z")}<span>Panel en vivo</span></a>
                    <a href="/odoo" target="_blank" rel="noopener">${icon("M14 3h7v7M10 14 21 3M21 14v7H3V3h7")}<span>Abrir Odoo</span></a>
                </div>
            </aside>
            <main class="td-main">
                <header class="td-top">
                    <label class="td-search">
                        ${icon("M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14zM21 21l-4.3-4.3")}
                        <input id="td-search" type="search" placeholder="Buscar instrumento o cartera" autocomplete="off"/>
                    </label>
                    <div class="td-top-right">
                        <span id="td-live" class="td-live"><i></i><span>Sin directo</span></span>
                        <button id="td-refresh" class="td-icon-btn" title="Actualizar ahora desde el broker">
                            ${icon("M21 12a9 9 0 1 1-2.64-6.36M21 3v6h-6")}
                        </button>
                        <div class="td-user"><div class="td-avatar">${esc(initials(USER))}</div><span>${esc(USER)}</span></div>
                    </div>
                </header>
                <section id="td-content" class="td-content"></section>
            </main>
        </div>`;
        document.getElementById("td-search").addEventListener("input", (ev) => {
            state.search = ev.target.value.trim().toLowerCase();
            if (state.search && !["posiciones", "mercado", "carteras", "operaciones"].includes(state.page)) {
                location.hash = "#/posiciones";
                return;
            }
            updatePage();
        });
        document.getElementById("td-refresh").addEventListener("click", refreshNow);
    }

    async function refreshNow() {
        const btn = document.getElementById("td-refresh");
        btn.classList.add("is-spinning");
        document.querySelectorAll("[data-action=refresh]").forEach((b) => (b.disabled = true));
        try {
            const res = await postRPC("/trading/api/refresh");
            toast(res.message || "Actualizado.", !res.ok);
            await poll(true);
            state.historyAt = 0;
            loadHistory();
        } catch (e) {
            toast(e.message, true);
        } finally {
            btn.classList.remove("is-spinning");
            document.querySelectorAll("[data-action=refresh]").forEach((b) => (b.disabled = false));
        }
    }

    // ------------------------------------------------------------------
    // Navegación
    // ------------------------------------------------------------------
    function route() {
        const key = (location.hash.replace(/^#\/?/, "") || "resumen").split("/")[0];
        state.page = PAGES.some((p) => p[0] === key) ? key : "resumen";
        document.querySelectorAll(".td-nav a").forEach((a) => a.classList.toggle("is-active", a.dataset.page === state.page));
        const label = PAGES.find((p) => p[0] === state.page)[1];
        document.title = `Trading · ${label}`;
        destroyChart();
        renderPage();
        if (state.page === "operaciones" && !state.closed) {
            loadClosed();
        }
        if (state.page === "renta" && !state.tax) {
            loadTax();
        }
        if (state.page === "resumen") {
            loadHistory();
        }
    }

    function renderPage() {
        const el = document.getElementById("td-content");
        if (!state.summary) {
            el.innerHTML = state.error
                ? `<div class="td-panel td-empty">${esc(state.error)}</div>`
                : `<div class="td-empty">Cargando…</div>`;
            return;
        }
        const pages = {
            resumen: pageOverview, posiciones: pagePositions, operaciones: pageClosed,
            carteras: pageMirrors, mercado: pageMarket, renta: pageTax,
        };
        el.innerHTML = pages[state.page]();
        bindPage();
        if (state.page === "resumen") {
            drawChart();
        }
    }

    /** Actualiza solo las zonas con datos vivos, sin rehacer la página (no se pierde el scroll ni la gráfica). */
    function updatePage() {
        if (!state.summary) {
            return renderPage();
        }
        const live = document.getElementById("td-live");
        if (live) {
            live.classList.toggle("is-on", !!state.summary.live);
            live.querySelector("span").textContent = state.summary.live ? "En directo" : "Sin directo";
        }
        const regions = document.querySelectorAll("[data-region]");
        if (!regions.length) {
            return renderPage();
        }
        regions.forEach((region) => {
            const fn = REGIONS[region.dataset.region];
            if (fn) {
                region.innerHTML = fn();
            }
        });
        bindRows();
        flash();
    }

    // ------------------------------------------------------------------
    // Resumen
    // ------------------------------------------------------------------
    function pageOverview() {
        return `
        <div class="td-page-head">
            <div><h1>Mis cuentas</h1><p>Valores en ${esc(CCY)} al cambio del día</p></div>
        </div>
        <div class="td-overview">
            <div class="td-col">
                <div class="td-panel" data-region="balance">${regionBalance()}</div>
                <div class="td-panel">
                    <div class="td-panel-head">
                        <div><h2>Evolución del resultado</h2><div class="td-sub" id="td-history-sub">Abierto + cerrado en el periodo</div></div>
                        <div class="td-seg" id="td-range">
                            ${[[7, "1S"], [30, "1M"], [90, "3M"], [365, "1A"]].map(([d, l]) =>
                                `<button data-days="${d}" class="${d === state.historyDays ? "is-active" : ""}">${l}</button>`).join("")}
                        </div>
                    </div>
                    <div id="td-chart" class="td-chart"></div>
                </div>
                <div class="td-panel">
                    <div class="td-panel-head"><h2>Acciones rápidas</h2></div>
                    <div class="td-quick">
                        <button class="td-btn primary" data-action="refresh">${icon("M21 12a9 9 0 1 1-2.64-6.36M21 3v6h-6")}Actualizar</button>
                        <button class="td-btn" data-go="posiciones">Posiciones</button>
                        <button class="td-btn" data-go="renta">Renta</button>
                        <button class="td-btn" data-odoo="action_trading_tax_report_wizard">PDF renta</button>
                    </div>
                </div>
            </div>
            <div class="td-col">
                <div class="td-cards" data-region="cards">${regionCards()}</div>
                <div class="td-panel" data-region="allocation">${regionAllocation()}</div>
            </div>
            <div class="td-col">
                <div class="td-panel">
                    <div class="td-panel-head"><h2>Últimos movimientos</h2><button class="td-link" data-go="operaciones">Ver todo</button></div>
                    <div class="td-list" data-region="activity">${regionActivity()}</div>
                </div>
                <div class="td-panel">
                    <div class="td-panel-head"><h2>Mejores y peores</h2><button class="td-link" data-go="posiciones">Ver todas</button></div>
                    <div class="td-list" data-region="movers">${regionMovers()}</div>
                </div>
            </div>
        </div>`;
    }

    function regionBalance() {
        const t = state.summary.totals;
        return `
            <div class="td-label">Patrimonio total</div>
            <div class="td-big" data-flash="equity" data-val="${t.equity}">${money(t.equity)}</div>
            <div class="td-delta ${cls(t.day)}">${signed(t.day)} · ${pct(t.day_pct)} <span>hoy</span></div>
            <div class="td-mini-stats">
                <div class="td-mini"><div class="td-label">Invertido</div><strong>${money(t.invested)}</strong></div>
                <div class="td-mini"><div class="td-label">Resultado abierto</div><strong class="${cls(t.unrealized)}">${signed(t.unrealized)}</strong></div>
                <div class="td-mini"><div class="td-label">Disponible</div><strong>${money(t.cash)}</strong></div>
            </div>`;
    }

    function regionCards() {
        const accounts = state.summary.accounts;
        if (!accounts.length) {
            return `<div class="td-panel td-empty">Sin cuentas de broker.</div>`;
        }
        return accounts.map((a) => `
            <div class="td-card ${a.environment === "real" ? "is-real" : ""}" data-odoo="action_trading_broker_account" data-id="${a.id}">
                <div class="td-card-top">
                    <div class="td-card-name">${esc(a.name)}<small>${esc((a.broker || "").toUpperCase())} · ${a.environment === "real" ? "Real" : "Demo"}${a.stream_live ? " · en directo" : ""}</small></div>
                    <div class="td-card-chip"></div>
                </div>
                <div class="td-card-amount">${money(a.equity, a.currency)}<small>patrimonio</small></div>
                <div class="td-card-foot">
                    <div>Disponible<strong>${money(a.credit, a.currency)}</strong></div>
                    <div>Abierto<strong class="${cls(a.unrealized_pnl)}">${signed(a.unrealized_pnl, a.currency)}</strong></div>
                    <div>${a.open_count} pos.<strong>${a.state === "error" ? '<span class="down">Error</span>' : "OK"}</strong></div>
                    <div class="td-card-dots"><i></i><i></i></div>
                </div>
            </div>`).join("");
    }

    function regionAllocation() {
        const alloc = state.summary.allocation;
        const total = alloc.reduce((s, a) => s + a.value, 0);
        if (!total) {
            return `<div class="td-panel-head"><h2>Reparto</h2></div><div class="td-empty">Sin posiciones abiertas.</div>`;
        }
        return `
            <div class="td-panel-head"><div><h2>Reparto</h2><div class="td-sub">${state.summary.totals.open_count} posiciones abiertas</div></div></div>
            <div class="td-alloc-bar">${alloc.map((a) => `<span style="width:${(100 * a.value) / total}%;background:${(ASSET[a.type] || ASSET.other)[1]}"></span>`).join("")}</div>
            <div class="td-legend">${alloc.map((a) => `
                <div><i style="background:${(ASSET[a.type] || ASSET.other)[1]}"></i>${(ASSET[a.type] || ASSET.other)[0]}<b>${num((100 * a.value) / total, 1)} %</b></div>`).join("")}
            </div>`;
    }

    function regionActivity() {
        const items = state.summary.activity;
        if (!items.length) {
            return `<div class="td-empty">Sin movimientos.</div>`;
        }
        return items.slice(0, 7).map((a) => {
            const isClose = a.kind === "close";
            const side = a.side === "buy" ? "Largo" : "Corto";
            return `
            <div class="td-item" data-odoo="action_trading_position" data-id="${a.id}">
                ${symBadge(a.symbol)}
                <div class="td-item-main"><strong>${esc(a.symbol)}${a.mirror ? ` <span class="td-pill accent">${esc(a.mirror)}</span>` : ""}</strong>
                    <small>${isClose ? "Cierre" : "Apertura"} · ${side} · ${fdate(a.date, true)}</small></div>
                <div class="td-item-end">${isClose
                    ? `<strong class="${cls(a.pnl)}">${signed(a.pnl, a.currency)}</strong><small>${a.estimated ? "estimado" : "cerrada"}</small>`
                    : `<strong>${money(a.amount, a.currency)}</strong><small>invertido</small>`}</div>
            </div>`;
        }).join("");
    }

    function regionMovers() {
        const ps = [...state.summary.positions].sort((a, b) => b.pnl_pct - a.pnl_pct);
        if (!ps.length) {
            return `<div class="td-empty">Sin posiciones abiertas.</div>`;
        }
        const pick = ps.length > 4 ? [...ps.slice(0, 2), ...ps.slice(-2)] : ps;
        return pick.map((p) => `
            <div class="td-item" data-odoo="action_trading_position" data-id="${p.id}">
                ${symBadge(p.symbol, p.asset_type)}
                <div class="td-item-main"><strong>${esc(p.symbol)}</strong><small>${esc(p.name || p.mirror || "")}</small></div>
                <div class="td-item-end"><strong class="${cls(p.unrealized_pnl)}">${signed(p.unrealized_pnl, p.currency)}</strong>
                    <small class="${cls(p.pnl_pct)}">${pct(p.pnl_pct)}</small></div>
            </div>`).join("");
    }

    // Gráfica de evolución
    function destroyChart() {
        if (state.chart) {
            state.chart.remove();
            state.chart = null;
            state.series = null;
        }
    }
    function drawChart() {
        const el = document.getElementById("td-chart");
        if (!el || !window.LightweightCharts) {
            return;
        }
        const points = state.history?.points || [];
        if (!points.length) {
            el.innerHTML = `<div class="td-chart-empty">${state.history ? "Sin datos en el periodo." : "Calculando…"}</div>`;
            return;
        }
        el.innerHTML = "";
        const LWC = window.LightweightCharts;
        const up = state.history.change >= 0;
        const line = up ? "#5b9dff" : "#f87171";
        state.chart = LWC.createChart(el, {
            autoSize: true,
            layout: { background: { type: "solid", color: "transparent" }, textColor: "#8d97ae", fontFamily: getComputedStyle(document.body).fontFamily, attributionLogo: false },
            grid: { vertLines: { visible: false }, horzLines: { color: "rgba(255,255,255,0.05)" } },
            rightPriceScale: { borderVisible: false, scaleMargins: { top: 0.15, bottom: 0.08 } },
            timeScale: { borderVisible: false, fixLeftEdge: true, fixRightEdge: true },
            crosshair: { vertLine: { color: "rgba(255,255,255,0.18)", labelBackgroundColor: "#1d2a4d" }, horzLine: { color: "rgba(255,255,255,0.18)", labelBackgroundColor: "#1d2a4d" } },
            handleScroll: false,
            handleScale: false,
            localization: { locale: "es-ES", priceFormatter: (v) => money(v, state.history.currency, 0) },
        });
        state.series = state.chart.addSeries(LWC.AreaSeries, {
            lineColor: line, lineWidth: 2,
            topColor: up ? "rgba(91,157,255,0.38)" : "rgba(248,113,113,0.32)",
            bottomColor: "rgba(91,157,255,0)",
            priceLineVisible: false,
            crosshairMarkerBorderColor: "#fff",
            crosshairMarkerBackgroundColor: line,
        });
        state.series.setData(points.map((p) => ({ time: p.time, value: p.value })));
        state.chart.timeScale().fitContent();
        const sub = document.getElementById("td-history-sub");
        if (sub) {
            sub.innerHTML = `<span class="${cls(state.history.change)}">${signed(state.history.change, state.history.currency)}</span> en el periodo · abierto + cerrado`;
        }
    }
    async function loadHistory(force) {
        if (state.page !== "resumen") {
            return;
        }
        if (!force && state.history && state.history.days === state.historyDays && Date.now() - state.historyAt < HISTORY_MS) {
            drawChart();
            return;
        }
        try {
            const data = await getJSON(`/trading/api/history?days=${state.historyDays}`);
            state.history = data;
            state.historyAt = Date.now();
            if (state.page === "resumen") {
                destroyChart();
                drawChart();
            }
        } catch (e) {
            const el = document.getElementById("td-chart");
            if (el) {
                el.innerHTML = `<div class="td-chart-empty">${esc(e.message)}</div>`;
            }
        }
    }

    // ------------------------------------------------------------------
    // Posiciones
    // ------------------------------------------------------------------
    function filteredPositions() {
        const q = state.search;
        let ps = state.summary.positions.filter((p) =>
            (state.posFilter === "all" || (state.posFilter === "copy" ? p.mirror : !p.mirror)) &&
            (!q || `${p.symbol} ${p.name} ${p.mirror}`.toLowerCase().includes(q)));
        const { key, dir } = state.sort;
        ps = ps.sort((a, b) => {
            const x = a[key], y = b[key];
            return (typeof x === "string" ? x.localeCompare(y) : (x || 0) - (y || 0)) * dir;
        });
        return ps;
    }
    const POS_COLS = [
        ["symbol", "Instrumento"], ["side", "Lado"], ["amount", "Invertido"], ["open_rate", "Apertura"],
        ["current_rate", "Actual"], ["change_pct", "Var. día"], ["unrealized_pnl", "Resultado"],
        ["pnl_pct", "%"], ["net_after_tax", "Neto tras IRPF"], ["current_value", "Valor"],
    ];
    function pagePositions() {
        return `
        <div class="td-page-head">
            <div><h1>Posiciones abiertas</h1><p>Se actualizan solas con los precios en directo</p></div>
            <div class="td-seg" id="td-pos-filter">
                ${[["all", "Todas"], ["direct", "Directas"], ["copy", "Copiadas"]].map(([k, l]) =>
                    `<button data-filter="${k}" class="${state.posFilter === k ? "is-active" : ""}">${l}</button>`).join("")}
            </div>
        </div>
        <div class="td-kpis" data-region="poskpis">${regionPosKpis()}</div>
        <div class="td-table-wrap">
            <table class="td-table">
                <thead><tr>${POS_COLS.map(([k, l]) => `<th data-sort="${k}" class="${state.sort.key === k ? "is-sorted" : ""}">${l}${state.sort.key === k ? (state.sort.dir > 0 ? " ↑" : " ↓") : ""}</th>`).join("")}</tr></thead>
                <tbody data-region="posrows">${regionPosRows()}</tbody>
            </table>
        </div>`;
    }
    function regionPosKpis() {
        const ps = filteredPositions();
        const sum = (k) => ps.reduce((s, p) => s + (p[k] || 0), 0);
        const ccy = ps[0]?.currency || "USD";
        const invested = sum("amount");
        const pnl = sum("unrealized_pnl");
        return `
            <div class="td-kpi"><div class="td-label">Posiciones</div><strong>${ps.length}</strong><small>${ps.filter((p) => p.unrealized_pnl > 0).length} en positivo</small></div>
            <div class="td-kpi"><div class="td-label">Invertido</div><strong>${money(invested, ccy)}</strong></div>
            <div class="td-kpi"><div class="td-label">Resultado abierto</div><strong class="${cls(pnl)}">${signed(pnl, ccy)}</strong><small class="${cls(pnl)}">${pct(invested ? (100 * pnl) / invested : 0)}</small></div>
            <div class="td-kpi"><div class="td-label">Hoy</div><strong class="${cls(sum("day_pnl"))}">${signed(sum("day_pnl"), ccy)}</strong></div>
            <div class="td-kpi"><div class="td-label">Neto si cerrase hoy</div><strong class="${cls(sum("net_after_tax"))}">${signed(sum("net_after_tax"))}</strong><small>IRPF ${money(sum("tax_estimate"))}</small></div>`;
    }
    function regionPosRows() {
        const ps = filteredPositions();
        if (!ps.length) {
            return `<tr><td colspan="${POS_COLS.length}" class="td-empty">Sin posiciones${state.search ? " con esa búsqueda" : ""}.</td></tr>`;
        }
        return ps.map((p) => {
            const w = Math.min(50, Math.abs(p.pnl_pct) / 2);
            return `
            <tr data-odoo="action_trading_position" data-id="${p.id}">
                <td><div class="td-inst">${symBadge(p.symbol, p.asset_type)}<div><strong>${esc(p.symbol)}</strong>
                    <small>${esc(p.mirror ? "Copia: " + p.mirror : p.name)}</small></div></div></td>
                <td><span class="td-pill ${p.side === "buy" ? "up" : "warn"}">${p.side === "buy" ? "Largo" : "Corto"}${p.leverage > 1 ? " x" + p.leverage : ""}</span></td>
                <td>${money(p.amount, p.currency)}</td>
                <td>${price(p.open_rate)}</td>
                <td data-flash="rate-${p.id}" data-val="${p.current_rate}">${price(p.current_rate)}</td>
                <td class="${cls(p.change_pct)}">${pct(p.change_pct)}</td>
                <td class="${cls(p.unrealized_pnl)}">${signed(p.unrealized_pnl, p.currency)}</td>
                <td class="${cls(p.pnl_pct)}">${pct(p.pnl_pct)}<span class="td-bar"><i class="${p.pnl_pct >= 0 ? "up" : "down"}" style="width:${w}%"></i></span></td>
                <td class="${cls(p.net_after_tax)}">${signed(p.net_after_tax)}</td>
                <td>${money(p.current_value, p.currency)}</td>
            </tr>`;
        }).join("");
    }

    // ------------------------------------------------------------------
    // Operaciones cerradas
    // ------------------------------------------------------------------
    function pageClosed() {
        const c = state.closed;
        return `
        <div class="td-page-head">
            <div><h1>Operaciones cerradas</h1><p>Resultado en ${esc(CCY)} con el IRPF estimado de su ejercicio</p></div>
            ${c ? `<select class="td-select" id="td-year">${c.years.map((y) => `<option ${y === c.year ? "selected" : ""}>${y}</option>`).join("")}</select>` : ""}
        </div>
        ${c ? closedBody(c) : `<div class="td-empty">Cargando…</div>`}`;
    }
    function closedBody(c) {
        const q = state.search;
        const rows = c.rows.filter((r) => !q || `${r.symbol} ${r.name} ${r.mirror}`.toLowerCase().includes(q));
        const t = c.totals;
        return `
        <div class="td-kpis">
            <div class="td-kpi"><div class="td-label">Operaciones</div><strong>${t.count}</strong><small>${t.wins} ganadoras · ${num(t.win_rate, 0)} %</small></div>
            <div class="td-kpi"><div class="td-label">Resultado</div><strong class="${cls(t.result)}">${signed(t.result)}</strong></div>
            <div class="td-kpi"><div class="td-label">IRPF estimado</div><strong>${money(t.tax)}</strong></div>
            <div class="td-kpi"><div class="td-label">Neto</div><strong class="${cls(t.net)}">${signed(t.net)}</strong></div>
        </div>
        <div class="td-table-wrap">
            <table class="td-table">
                <thead><tr><th>Instrumento</th><th>Lado</th><th>Apertura</th><th>Cierre</th><th>Precio entrada</th><th>Precio salida</th><th>Resultado</th><th>${esc(CCY)}</th><th>IRPF</th><th>Neto</th></tr></thead>
                <tbody>${rows.length ? rows.map((r) => `
                    <tr data-odoo="action_trading_position" data-id="${r.id}">
                        <td><div class="td-inst">${symBadge(r.symbol)}<div><strong>${esc(r.symbol)}</strong><small>${esc(r.mirror ? "Copia: " + r.mirror : r.name)}</small></div></div></td>
                        <td><span class="td-pill ${r.side === "buy" ? "up" : "warn"}">${r.side === "buy" ? "Largo" : "Corto"}</span></td>
                        <td>${fdate(r.open_date)}</td>
                        <td>${fdate(r.close_date, true)}${r.estimated ? ' <span class="td-pill warn" title="Cierre estimado: importe el extracto de eToro">est.</span>' : ""}</td>
                        <td>${price(r.open_rate)}</td>
                        <td>${price(r.close_rate)}</td>
                        <td class="${cls(r.realized_pnl)}">${signed(r.realized_pnl, r.currency)}</td>
                        <td class="${cls(r.realized_pnl_company)}">${signed(r.realized_pnl_company)}</td>
                        <td>${money(r.tax_estimate)}</td>
                        <td class="${cls(r.net_after_tax)}">${signed(r.net_after_tax)}</td>
                    </tr>`).join("") : `<tr><td colspan="10" class="td-empty">Sin operaciones cerradas en ${c.year}.</td></tr>`}
                </tbody>
                ${rows.length ? `<tfoot><tr><td colspan="7">Total</td><td class="${cls(t.result)}">${signed(t.result)}</td><td>${money(t.tax)}</td><td class="${cls(t.net)}">${signed(t.net)}</td></tr></tfoot>` : ""}
            </table>
        </div>`;
    }
    async function loadClosed(year) {
        try {
            state.closed = await getJSON(`/trading/api/closed${year ? "?year=" + year : ""}`);
        } catch (e) {
            toast(e.message, true);
        }
        if (state.page === "operaciones") {
            renderPage();
        }
    }

    // ------------------------------------------------------------------
    // Carteras copiadas
    // ------------------------------------------------------------------
    function pageMirrors() {
        return `
        <div class="td-page-head"><div><h1>Carteras copiadas</h1><p>Copy trading y Smart Portfolios</p></div></div>
        <div class="td-grid" data-region="mirrors">${regionMirrors()}</div>`;
    }
    function regionMirrors() {
        const q = state.search;
        const ms = state.summary.mirrors.filter((m) => !q || m.name.toLowerCase().includes(q));
        if (!ms.length) {
            return `<div class="td-panel td-empty">Sin carteras copiadas.</div>`;
        }
        return ms.map((m) => `
            <div class="td-tile" data-odoo="action_trading_mirror" data-id="${m.id}">
                <div class="td-tile-head">${symBadge(m.name, "other")}<div><strong>${esc(m.name)}</strong>
                    <small>${m.open_count} posiciones${m.paused ? " · en pausa" : ""}${m.start_date ? " · desde " + fdate(m.start_date) : ""}</small></div></div>
                <div class="td-tile-price">${money(m.value, m.currency)}</div>
                <div class="td-delta ${cls(m.unrealized_pnl)}">${signed(m.unrealized_pnl, m.currency)} · ${pct(m.pnl_pct)} <span>abierto</span></div>
                <div class="td-tile-rows">
                    <div>Invertido<strong>${money(m.invested, m.currency)}</strong></div>
                    <div>Resultado total<strong class="${cls(m.total_pnl)}">${signed(m.total_pnl, m.currency)}</strong></div>
                </div>
                <div class="td-progress"><i style="width:${Math.max(4, Math.min(100, m.invested ? (100 * m.value) / (m.invested * 1.5) : 0))}%"></i></div>
            </div>`).join("");
    }

    // ------------------------------------------------------------------
    // Mercado
    // ------------------------------------------------------------------
    function pageMarket() {
        return `
        <div class="td-page-head"><div><h1>Seguimiento</h1><p>Instrumentos marcados «En seguimiento»</p></div></div>
        <div class="td-grid" data-region="market">${regionMarket()}</div>`;
    }
    function regionMarket() {
        const q = state.search;
        const ws = state.summary.watchlist.filter((w) => !q || `${w.symbol} ${w.name}`.toLowerCase().includes(q));
        if (!ws.length) {
            return `<div class="td-panel td-empty">Nada en seguimiento.</div>`;
        }
        return ws.map((w) => `
            <div class="td-tile" data-odoo="action_trading_instrument" data-id="${w.id}">
                <div class="td-tile-head">${symBadge(w.symbol, w.asset_type)}<div><strong>${esc(w.symbol)}</strong><small>${esc(w.name || (ASSET[w.asset_type] || ASSET.other)[0])}</small></div>
                    <span class="td-pill ${cls(w.change_pct)}">${pct(w.change_pct)}</span></div>
                <div class="td-tile-price" data-flash="inst-${w.id}" data-val="${w.last_price}">${price(w.last_price)}</div>
                <div class="td-tile-rows">
                    <div>Compra (bid)<strong>${price(w.bid)}</strong></div>
                    <div>Venta (ask)<strong>${price(w.ask)}</strong></div>
                </div>
            </div>`).join("");
    }

    // ------------------------------------------------------------------
    // Renta
    // ------------------------------------------------------------------
    function pageTax() {
        const t = state.tax;
        const thisYear = new Date().getFullYear();
        const years = [thisYear, thisYear - 1, thisYear - 2];
        return `
        <div class="td-page-head">
            <div><h1>Renta ${state.taxYear}</h1><p>Base del ahorro del IRPF · estimación orientativa</p></div>
            <div style="display:flex;gap:10px;flex-wrap:wrap">
                <select class="td-select" id="td-tax-year">${years.map((y) => `<option ${y === state.taxYear ? "selected" : ""}>${y}</option>`).join("")}</select>
                <button class="td-btn primary" data-odoo="action_trading_tax_report_wizard">Informe PDF</button>
            </div>
        </div>
        ${t ? taxBody(t) : `<div class="td-empty">Calculando…</div>`}`;
    }
    function taxBody(t) {
        const blocks = [
            ["securities", "Acciones y ETF", "#4f8cff"], ["crypto", "Cripto", "#f59e0b"], ["cfd", "Derivados / CFD", "#a78bfa"],
            ["dividends", "Dividendos", "#34d399"], ["interest", "Intereses", "#22d3ee"],
        ];
        const max = Math.max(1, ...blocks.map(([k]) => Math.abs(t.blocks[k] || 0)));
        return `
        <div class="td-kpis">
            <div class="td-kpi"><div class="td-label">Resultado bruto</div><strong class="${cls(t.gross_result)}">${signed(t.gross_result, t.currency)}</strong></div>
            <div class="td-kpi"><div class="td-label">IRPF a pagar en España</div><strong>${money(t.tax, t.currency)}</strong><small>Retenciones fuera ${money(t.foreign_withholding, t.currency)}</small></div>
            <div class="td-kpi"><div class="td-label">Neto después de impuestos</div><strong class="${cls(t.net_result)}">${signed(t.net_result, t.currency)}</strong></div>
            <div class="td-kpi"><div class="td-label">Tipo efectivo · marginal</div><strong>${num(t.effective_rate, 1)} % · ${num(t.marginal_rate, 0)} %</strong></div>
        </div>
        <div class="td-tax-hero">
            <div class="td-panel">
                <div class="td-panel-head"><h2>Por tipo de renta</h2></div>
                <div class="td-blocks">${blocks.map(([k, l, c]) => {
                    const v = t.blocks[k] || 0;
                    const w = (50 * Math.abs(v)) / max;
                    return `<div class="td-block-row"><span>${l} <span class="muted">(${t.counts[k] || 0})</span></span>
                        <div class="td-track"><i style="background:${v < 0 ? "var(--td-down)" : c};${v < 0 ? `right:50%` : `left:50%`};width:${w}%"></i></div>
                        <b class="${cls(v)}">${signed(v, t.currency)}</b></div>`;
                }).join("")}</div>
                ${t.warnings.length ? `<div class="td-note">${t.warnings.map(esc).join("<br/>")}</div>` : ""}
            </div>
            <div class="td-panel">
                <div class="td-panel-head"><h2>Cálculo</h2></div>
                <div class="td-steps">
                    <div class="td-step"><span>Resultado bruto</span><b>${signed(t.gross_result, t.currency)}</b></div>
                    <div class="td-step"><span>Base del ahorro</span><b>${money(t.base, t.currency)}</b></div>
                    <div class="td-step"><span>Deducción doble imposición</span><b>−${money(t.deduction, t.currency)}</b></div>
                    <div class="td-step"><span>IRPF estimado</span><b>${money(t.tax, t.currency)}</b></div>
                    ${t.carry_forward ? `<div class="td-step"><span>Pérdidas para años siguientes</span><b>${money(t.carry_forward, t.currency)}</b></div>` : ""}
                    <div class="td-step"><span>Neto</span><b class="${cls(t.net_result)}">${signed(t.net_result, t.currency)}</b></div>
                </div>
                <div class="td-note">Estimación con la escala del ahorro (${t.scale.map(([u, r]) => `${num(r, 0)} %`).join(", ")}).
                    No incluye pérdidas de años anteriores ni otras rentas${t.other_savings ? ` salvo ${money(t.other_savings, t.currency)} indicados` : ""}. Revísela con su asesor.</div>
            </div>
        </div>`;
    }
    async function loadTax() {
        try {
            state.tax = await getJSON(`/trading/api/tax?year=${state.taxYear}`);
        } catch (e) {
            toast(e.message, true);
        }
        if (state.page === "renta") {
            renderPage();
        }
    }

    const REGIONS = {
        balance: regionBalance, cards: regionCards, allocation: regionAllocation, activity: regionActivity,
        movers: regionMovers, poskpis: regionPosKpis, posrows: regionPosRows, mirrors: regionMirrors, market: regionMarket,
    };

    // ------------------------------------------------------------------
    // Eventos
    // ------------------------------------------------------------------
    function bindRows() {
        document.querySelectorAll("#td-content [data-odoo]").forEach((el) => {
            if (el.dataset.bound) {
                return;
            }
            el.dataset.bound = "1";
            el.addEventListener("click", () => openOdoo(el.dataset.odoo, el.dataset.id));
        });
    }
    function bindPage() {
        bindRows();
        document.querySelectorAll("[data-go]").forEach((el) => el.addEventListener("click", () => (location.hash = "#/" + el.dataset.go)));
        document.querySelectorAll("[data-action=refresh]").forEach((el) => el.addEventListener("click", refreshNow));
        document.querySelectorAll("#td-range button").forEach((b) => b.addEventListener("click", () => {
            state.historyDays = Number(b.dataset.days);
            document.querySelectorAll("#td-range button").forEach((x) => x.classList.toggle("is-active", x === b));
            destroyChart();
            const el = document.getElementById("td-chart");
            if (el) {
                el.innerHTML = `<div class="td-chart-empty">Calculando…</div>`;
            }
            loadHistory(true);
        }));
        document.querySelectorAll("#td-pos-filter button").forEach((b) => b.addEventListener("click", () => {
            state.posFilter = b.dataset.filter;
            renderPage();
        }));
        document.querySelectorAll(".td-table th[data-sort]").forEach((th) => th.addEventListener("click", () => {
            const key = th.dataset.sort;
            state.sort = { key, dir: state.sort.key === key ? -state.sort.dir : (key === "symbol" || key === "side" ? 1 : -1) };
            renderPage();
        }));
        const year = document.getElementById("td-year");
        if (year) {
            year.addEventListener("change", () => loadClosed(year.value));
        }
        const taxYear = document.getElementById("td-tax-year");
        if (taxYear) {
            taxYear.addEventListener("change", () => {
                state.taxYear = Number(taxYear.value);
                state.tax = null;
                renderPage();
                loadTax();
            });
        }
    }

    /** Destello verde o rojo en los valores que han cambiado desde la última consulta. */
    function flash() {
        document.querySelectorAll("[data-flash]").forEach((el) => {
            const key = el.dataset.flash;
            const val = Number(el.dataset.val);
            const old = state.prev[key];
            if (old !== undefined && old !== val) {
                el.classList.remove("td-flash-up", "td-flash-down");
                void el.offsetWidth;
                el.classList.add(val > old ? "td-flash-up" : "td-flash-down");
            }
            state.prev[key] = val;
        });
    }

    // ------------------------------------------------------------------
    // Consulta periódica
    // ------------------------------------------------------------------
    let polling = false;
    async function poll(force) {
        if (polling || (!force && document.hidden)) {
            return;
        }
        polling = true;
        try {
            const first = !state.summary;
            state.summary = await getJSON("/trading/api/summary");
            state.error = "";
            if (first) {
                renderPage();
                flash();
            } else {
                updatePage();
            }
            if (state.page === "resumen" && Date.now() - state.historyAt > HISTORY_MS) {
                loadHistory();
            }
        } catch (e) {
            state.error = e.message;
            if (!state.summary) {
                renderPage();
            }
        } finally {
            polling = false;
        }
    }

    renderShell();
    window.addEventListener("hashchange", route);
    document.addEventListener("visibilitychange", () => !document.hidden && poll(true));
    route();
    poll(true);
    setInterval(poll, POLL_MS);
})();
