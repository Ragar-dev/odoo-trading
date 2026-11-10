import {
    Component, onMounted, onWillStart, onWillUnmount, onWillUpdateProps, useExternalListener, useRef, useState,
} from "@odoo/owl";
import { registry } from "@web/core/registry";
import { loadJS } from "@web/core/assets";
import { cookie } from "@web/core/browser/cookie";
import { useService } from "@web/core/utils/hooks";
import { useTradingLive } from "../live/trading_live_service";
import { ConfirmationDialog } from "@web/core/confirmation_dialog/confirmation_dialog";

const LIB_URL = "/trading_base/static/lib/lightweight-charts/lightweight-charts.standalone.production.js";
const TZ = "Atlantic/Canary";

const INTERVALS = [
    { key: "FifteenMinutes", label: "15m" },
    { key: "OneHour", label: "1h" },
    { key: "FourHours", label: "4h" },
    { key: "OneDay", label: "1D" },
    { key: "OneWeek", label: "1S" },
];

const TOOLS = [
    { key: "cursor", icon: "fa-mouse-pointer", title: "Mover y hacer zoom", hint: "" },
    { key: "hline", icon: "fa-arrows-h", title: "Línea horizontal", hint: "Haga clic en el precio de la línea." },
    { key: "trend", icon: "fa-line-chart", title: "Línea de tendencia", hint: "Clic en el punto inicial y otro en el final." },
    { key: "rect", icon: "fa-square-o", title: "Rectángulo (zona)", hint: "Clic en una esquina y otro en la opuesta." },
    { key: "text", icon: "fa-font", title: "Nota de texto", hint: "Escriba la nota y haga clic donde quiera dejarla." },
    { key: "erase", icon: "fa-eraser", title: "Borrar un dibujo", hint: "Haga clic sobre el dibujo que quiere borrar." },
];

const COLORS = ["#2962ff", "#26a69a", "#ef5350", "#ff9800", "#ab47bc", "#787b86"];

// Vista de la gráfica (rango e intervalo): solo en este navegador.
const STORE = "trading_chart";
function storeGet(key) {
    try {
        const raw = window.localStorage.getItem(`${STORE}:${key}`);
        return raw ? JSON.parse(raw) : null;
    } catch {
        return null;
    }
}
function storeSet(key, value) {
    try {
        window.localStorage.setItem(`${STORE}:${key}`, JSON.stringify(value));
    } catch {
        // Almacenamiento bloqueado (modo privado): la gráfica funciona igual.
    }
}

function hexToRgba(hex, alpha) {
    const h = (hex || "#2962ff").replace("#", "");
    const n = parseInt(h.length === 3 ? h.split("").map((c) => c + c).join("") : h, 16);
    return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
}

function distToSegment(px, py, x1, y1, x2, y2) {
    const dx = x2 - x1;
    const dy = y2 - y1;
    const len = dx * dx + dy * dy;
    let t = len ? ((px - x1) * dx + (py - y1) * dy) / len : 0;
    t = Math.max(0, Math.min(1, t));
    return Math.hypot(px - (x1 + t * dx), py - (y1 + t * dy));
}

/** Primitiva de Lightweight Charts que pinta tendencias, rectángulos y notas. */
class DrawingsPrimitive {
    constructor(widget) {
        this.w = widget;
        this.requestUpdate = () => {};
        this._view = {
            zOrder: () => "top",
            renderer: () => ({ draw: (target) => this.draw(target) }),
        };
    }
    attached({ requestUpdate }) {
        this.requestUpdate = requestUpdate;
    }
    detached() {
        this.requestUpdate = () => {};
    }
    updateAllViews() {}
    paneViews() {
        return [this._view];
    }
    draw(target) {
        target.useMediaCoordinateSpace(({ context: ctx }) => {
            for (const d of this.w.drawings) {
                if (d.kind !== "hline") {
                    this.drawOne(ctx, d, false);
                }
            }
            const preview = this.w.previewDrawing();
            if (preview) {
                this.drawOne(ctx, preview, true);
            }
        });
    }
    drawOne(ctx, d, preview) {
        const p1 = this.w.toXY(d.time1, d.price1);
        if (!p1) {
            return;
        }
        ctx.save();
        ctx.strokeStyle = d.color;
        ctx.fillStyle = d.color;
        ctx.lineWidth = 2;
        if (preview) {
            ctx.setLineDash([5, 4]);
        }
        if (d.kind === "text") {
            ctx.font = "600 12px system-ui, sans-serif";
            const text = d.text || "Nota";
            const width = ctx.measureText(text).width;
            ctx.fillStyle = hexToRgba(d.color, 0.15);
            ctx.fillRect(p1.x - 4, p1.y - 14, width + 8, 18);
            ctx.fillStyle = d.color;
            ctx.fillText(text, p1.x, p1.y);
            ctx.restore();
            return;
        }
        const p2 = this.w.toXY(d.time2, d.price2);
        if (!p2) {
            ctx.restore();
            return;
        }
        if (d.kind === "trend") {
            ctx.beginPath();
            ctx.moveTo(p1.x, p1.y);
            ctx.lineTo(p2.x, p2.y);
            ctx.stroke();
            for (const p of [p1, p2]) {
                ctx.beginPath();
                ctx.arc(p.x, p.y, 3, 0, 2 * Math.PI);
                ctx.fill();
            }
        } else if (d.kind === "rect") {
            const x = Math.min(p1.x, p2.x);
            const y = Math.min(p1.y, p2.y);
            const w = Math.abs(p2.x - p1.x);
            const h = Math.abs(p2.y - p1.y);
            ctx.fillStyle = hexToRgba(d.color, 0.15);
            ctx.fillRect(x, y, w, h);
            ctx.lineWidth = 1;
            ctx.strokeRect(x, y, w, h);
        }
        ctx.restore();
    }
}

export class TradingChart extends Component {
    static template = "trading_base.TradingChart";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.dialog = useService("dialog");
        this.notification = useService("notification");
        this.chartRef = useRef("chart");
        this.intervals = INTERVALS;
        this.tools = TOOLS;
        this.colors = COLORS;
        this.state = useState({
            interval: "OneDay",
            tool: "cursor",
            color: COLORS[0],
            noteText: "",
            loading: false,
            error: "",
            empty: false,
            pending: false,
            count: 0,
            marksPanel: false,
            prefs: {
                show_markers: true, show_lines: true,
                buy_color: "#26a69a", sell_color: "#ef5350", marker_size: 1,
            },
        });
        this.positions = [];
        this.restoring = false;
        this.drawings = [];
        this.times = [];
        this.step = 86400;
        this.priceLines = new Map();
        this.markLines = [];
        this.pending = null;
        this.hover = null;
        useExternalListener(window, "keydown", (ev) => {
            if (ev.key === "Escape" && this.pending) {
                this.cancelPending();
            }
        });
        onWillStart(() => loadJS(LIB_URL));
        onMounted(() => {
            this.createChart();
            this.load();
            this.liveTimer = setInterval(() => this.refreshLastPrice(), 30000);
        });
        // Precio en directo del instrumento (WebSocket del broker → bus de Odoo).
        useTradingLive((kind, payload) => {
            const inst = kind === "prices" && payload.instruments?.find((i) => i.id === this.instrumentId);
            if (inst && inst.last_price && inst.last_price !== this.lastPrice && this.series && !this.state.loading) {
                this.lastPrice = inst.last_price;
                this.applyLastPrice(inst.last_price);
            }
        }, 500);
        onWillUpdateProps((next) => {
            if (this.idsOf(next).instrumentId !== this.idsOf(this.props).instrumentId) {
                this.nextProps = next;
                Promise.resolve().then(() => this.load());
            }
        });
        onWillUnmount(() => {
            clearInterval(this.liveTimer);
            clearTimeout(this.rangeTimer);
            if (this.chart) {
                this.chart.remove();
            }
        });
    }

    // ------------------------------------------------------------------
    idsOf(props) {
        const record = props.record;
        if (!record) {
            return { instrumentId: false, positionId: false };
        }
        if (record.resModel === "trading.instrument") {
            return { instrumentId: record.resId || false, positionId: false };
        }
        const value = record.data.instrument_id;
        const instrumentId = Array.isArray(value) ? value[0] : value && value.id;
        return {
            instrumentId: instrumentId || false,
            positionId: record.resModel === "trading.position" ? record.resId || false : false,
        };
    }

    get ids() {
        return this.idsOf(this.nextProps || this.props);
    }

    get hint() {
        if (this.state.tool === "erase" && !this.state.count) {
            return "No hay dibujos que borrar.";
        }
        const tool = TOOLS.find((t) => t.key === this.state.tool);
        return this.state.pending ? "Ahora el segundo punto (Esc para cancelar)." : tool ? tool.hint : "";
    }

    // ------------------------------------------------------------------
    createChart() {
        const LWC = window.LightweightCharts;
        const dark = cookie.get("color_scheme") === "dark";
        const text = dark ? "#d1d4dc" : "#333";
        const grid = dark ? "rgba(255,255,255,0.06)" : "rgba(0,0,0,0.06)";
        this.chart = LWC.createChart(this.chartRef.el, {
            autoSize: true,
            layout: { background: { type: "solid", color: dark ? "#1e1e24" : "#ffffff" }, textColor: text },
            grid: { vertLines: { color: grid }, horzLines: { color: grid } },
            crosshair: { mode: LWC.CrosshairMode.Normal },
            rightPriceScale: { borderVisible: false },
            timeScale: {
                borderVisible: false,
                rightOffset: 8,
                tickMarkFormatter: (time, type) => this.formatTick(time, type),
            },
            localization: { locale: "es-ES", timeFormatter: (time) => this.formatTime(time) },
        });
        this.series = this.chart.addSeries(LWC.CandlestickSeries, {
            upColor: "#26a69a", downColor: "#ef5350", borderVisible: false,
            wickUpColor: "#26a69a", wickDownColor: "#ef5350",
        });
        this.volume = this.chart.addSeries(LWC.HistogramSeries, {
            priceFormat: { type: "volume" }, priceScaleId: "", lastValueVisible: false, priceLineVisible: false,
        });
        this.volume.priceScale().applyOptions({ scaleMargins: { top: 0.85, bottom: 0 } });
        this.markers = LWC.createSeriesMarkers(this.series, []);
        this.primitive = new DrawingsPrimitive(this);
        this.series.attachPrimitive(this.primitive);
        this.chart.subscribeClick((param) => this.onClick(param));
        this.chart.timeScale().subscribeVisibleLogicalRangeChange((range) => this.onRangeChange(range));
        this.chart.subscribeCrosshairMove((param) => this.onMove(param));
    }

    formatTime(time) {
        const opts = { timeZone: TZ, day: "2-digit", month: "short", year: "2-digit" };
        if (this.step < 86400) {
            Object.assign(opts, { hour: "2-digit", minute: "2-digit" });
        }
        return new Intl.DateTimeFormat("es-ES", opts).format(new Date(time * 1000));
    }

    formatTick(time, type) {
        const date = new Date(time * 1000);
        const fmt = (o) => new Intl.DateTimeFormat("es-ES", { timeZone: TZ, ...o }).format(date);
        if (type === 0) {
            return fmt({ year: "numeric" });
        }
        if (type === 1) {
            return fmt({ month: "short" });
        }
        if (type === 2) {
            return fmt({ day: "numeric" });
        }
        return fmt({ hour: "2-digit", minute: "2-digit" });
    }

    // ------------------------------------------------------------------
    async load() {
        const { instrumentId, positionId } = this.ids;
        this.nextProps = null;
        if (!this.chart) {
            return;
        }
        if (!instrumentId) {
            this.state.error = "Guarde el registro para ver la gráfica.";
            return;
        }
        if (instrumentId !== this.instrumentId) {
            const saved = storeGet(`interval:${instrumentId}`);
            if (saved && this.intervals.some((i) => i.key === saved)) {
                this.state.interval = saved;
            }
        }
        this.state.loading = true;
        this.state.error = "";
        let data;
        try {
            data = await this.orm.call("trading.instrument", "get_chart_data", [[instrumentId]], {
                interval: this.state.interval,
                position_id: positionId,
            });
        } catch (e) {
            this.state.loading = false;
            this.state.error = "No se pudieron cargar los datos de la gráfica.";
            throw e;
        }
        if (!this.chart) {
            return;
        }
        this.instrumentId = instrumentId;
        this.step = data.step;
        this.times = data.candles.map((c) => c.time);
        this.lastPrice = data.last_price;
        const last = data.candles.length ? data.candles[data.candles.length - 1].close : data.last_price;
        const precision = last && last < 1 ? 5 : last && last < 10 ? 4 : 2;
        this.series.applyOptions({ priceFormat: { type: "price", precision, minMove: 10 ** -precision } });
        this.series.setData(data.candles.map(({ time, open, high, low, close }) => ({ time, open, high, low, close })));
        this.volume.setData(data.candles.map((c) => ({
            time: c.time, value: c.volume,
            color: c.close >= c.open ? "rgba(38,166,154,0.35)" : "rgba(239,83,80,0.35)",
        })));
        this.applyLastPrice(data.last_price);
        this.state.empty = !data.candles.length;
        this.state.error = data.error ? `Broker: ${data.error}` : "";
        Object.assign(this.state.prefs, data.prefs || {});
        this.positions = data.positions;
        this.setPositions();
        this.drawings = data.drawings;
        this.renderHLines();
        this.state.count = this.drawings.length;
        this.restoreRange(data.positions);
        this.state.loading = false;
        this.primitive.requestUpdate();
    }

    // ------------------------------------------------------------------
    // Rango visible: se recuerda por instrumento e intervalo, en tiempos (no en índices),
    // para que siga valiendo cuando llegan velas nuevas.
    get rangeKey() {
        return `range:${this.instrumentId}:${this.state.interval}`;
    }

    restoreRange(positions) {
        const n = this.times.length;
        this.restoring = true;
        const saved = storeGet(this.rangeKey);
        if (n && saved && saved.from < saved.to) {
            this.chart.timeScale().setVisibleLogicalRange({
                from: this.timeToLogical(saved.from), to: this.timeToLogical(saved.to),
            });
        } else if (n > 150) {
            let from = n - 150;
            const focus = positions.find((p) => p.focus && p.open_time);
            if (focus) {
                from = Math.min(from, Math.floor(this.timeToLogical(focus.open_time)) - 10);
            }
            this.chart.timeScale().setVisibleLogicalRange({ from: Math.max(from, -5), to: n + 8 });
        } else {
            this.chart.timeScale().fitContent();
        }
        // El cambio de rango se notifica de forma asíncrona: no guardarlo como si fuera del usuario.
        setTimeout(() => (this.restoring = false), 300);
    }

    onRangeChange(range) {
        if (this.restoring || this.state.loading || !range || !this.times.length || !this.instrumentId) {
            return;
        }
        clearTimeout(this.rangeTimer);
        this.rangeTimer = setTimeout(() => {
            storeSet(this.rangeKey, { from: this.logicalToTime(range.from), to: this.logicalToTime(range.to) });
        }, 400);
    }

    resetRange() {
        try {
            window.localStorage.removeItem(`${STORE}:${this.rangeKey}`);
        } catch {
            // nada que limpiar
        }
        this.restoreRange(this.positions);
    }

    applyLastPrice(price) {
        if (!price || !this.times.length) {
            return;
        }
        const data = this.series.data();
        const bar = data[data.length - 1];
        if (!bar) {
            return;
        }
        this.series.update({
            time: bar.time, open: bar.open, close: price,
            high: Math.max(bar.high, price), low: Math.min(bar.low, price),
        });
    }

    async refreshLastPrice() {
        if (document.hidden || !this.instrumentId || this.state.loading) {
            return;
        }
        const [rec] = await this.orm.read("trading.instrument", [this.instrumentId], ["last_price"]);
        if (rec && rec.last_price && rec.last_price !== this.lastPrice) {
            this.lastPrice = rec.last_price;
            this.applyLastPrice(rec.last_price);
        }
    }

    setPositions() {
        const positions = this.positions;
        const prefs = this.state.prefs;
        for (const line of this.markLines) {
            this.series.removePriceLine(line);
        }
        this.markLines = [];
        const markers = [];
        for (const p of positions) {
            const showLines = p.focus || p.state === "open";
            const label = p.focus ? "" : ` ${p.name.split(" ")[0]}`;
            if (prefs.show_lines && showLines && p.open_rate) {
                this.addMarkLine(p.open_rate, "#2962ff", `Entrada${label}`, 2);
                if (p.stop_loss) {
                    this.addMarkLine(p.stop_loss, "#ef5350", "Stop", 1);
                }
                if (p.take_profit) {
                    this.addMarkLine(p.take_profit, "#26a69a", "TP", 1);
                }
            }
            if (!prefs.show_markers) {
                continue;
            }
            const openBar = this.snapTime(p.open_time);
            if (openBar !== null) {
                const buy = p.side === "buy";
                markers.push({
                    time: openBar,
                    position: buy ? "belowBar" : "aboveBar",
                    shape: buy ? "arrowUp" : "arrowDown",
                    color: buy ? prefs.buy_color : prefs.sell_color,
                    text: buy ? "Compra" : "Venta",
                    size: prefs.marker_size,
                });
            }
            const closeBar = this.snapTime(p.close_time);
            if (p.close_time && closeBar !== null) {
                markers.push({
                    time: closeBar, position: "aboveBar", shape: "circle", color: "#787b86",
                    text: "Cierre", size: prefs.marker_size,
                });
            }
        }
        markers.sort((a, b) => a.time - b.time);
        this.markers.setMarkers(markers);
    }

    addMarkLine(price, color, title, style) {
        this.markLines.push(this.series.createPriceLine({
            price, color, title, lineWidth: 1, lineStyle: style, axisLabelVisible: true,
        }));
    }

    renderHLines() {
        for (const line of this.priceLines.values()) {
            this.series.removePriceLine(line);
        }
        this.priceLines.clear();
        for (const d of this.drawings) {
            if (d.kind === "hline") {
                this.priceLines.set(d.id, this.series.createPriceLine({
                    price: d.price1, color: d.color, lineWidth: 2, lineStyle: 0,
                    axisLabelVisible: true, title: d.text || "",
                }));
            }
        }
    }

    // ------------------------------------------------------------------
    // Conversión tiempo <-> coordenada, válida en cualquier intervalo y fuera de los datos.
    snapTime(t) {
        const times = this.times;
        if (!t || !times.length || t < times[0]) {
            return null;
        }
        let lo = 0;
        let hi = times.length - 1;
        if (t >= times[hi]) {
            return times[hi];
        }
        while (hi - lo > 1) {
            const mid = (lo + hi) >> 1;
            if (times[mid] <= t) {
                lo = mid;
            } else {
                hi = mid;
            }
        }
        return times[lo];
    }

    timeToLogical(t) {
        const times = this.times;
        const n = times.length;
        if (!n) {
            return null;
        }
        if (t <= times[0]) {
            return (t - times[0]) / this.step;
        }
        if (t >= times[n - 1]) {
            return n - 1 + (t - times[n - 1]) / this.step;
        }
        let lo = 0;
        let hi = n - 1;
        while (hi - lo > 1) {
            const mid = (lo + hi) >> 1;
            if (times[mid] <= t) {
                lo = mid;
            } else {
                hi = mid;
            }
        }
        return lo + (t - times[lo]) / (times[hi] - times[lo]);
    }

    logicalToTime(logical) {
        const times = this.times;
        const n = times.length;
        if (!n) {
            return null;
        }
        if (logical <= 0) {
            return Math.round(times[0] + logical * this.step);
        }
        if (logical >= n - 1) {
            return Math.round(times[n - 1] + (logical - (n - 1)) * this.step);
        }
        const i = Math.floor(logical);
        return Math.round(times[i] + (logical - i) * (times[i + 1] - times[i]));
    }

    toXY(time, price) {
        const logical = this.timeToLogical(time);
        if (logical === null) {
            return null;
        }
        const x = this.chart.timeScale().logicalToCoordinate(logical);
        const y = this.series.priceToCoordinate(price);
        return x === null || y === null ? null : { x, y };
    }

    pointOf(param) {
        if (!param.point || !this.times.length) {
            return null;
        }
        const logical = this.chart.timeScale().coordinateToLogical(param.point.x);
        const price = this.series.coordinateToPrice(param.point.y);
        if (logical === null || price === null) {
            return null;
        }
        return { time: this.logicalToTime(logical), price, x: param.point.x, y: param.point.y };
    }

    // ------------------------------------------------------------------
    previewDrawing() {
        if (!this.pending || !this.hover) {
            return null;
        }
        return { ...this.pending, time2: this.hover.time, price2: this.hover.price };
    }

    onMove(param) {
        if (!this.pending) {
            return;
        }
        this.hover = this.pointOf(param);
        this.primitive.requestUpdate();
    }

    cancelPending() {
        this.pending = null;
        this.hover = null;
        this.state.pending = false;
        this.primitive.requestUpdate();
    }

    setTool(tool) {
        this.cancelPending();
        this.state.tool = tool;
    }

    setColor(color) {
        this.state.color = color;
    }

    // ------------------------------------------------------------------
    toggleMarksPanel() {
        this.state.marksPanel = !this.state.marksPanel;
    }

    setPref(key, value) {
        this.state.prefs[key] = value;
        this.setPositions();
        clearTimeout(this.prefsTimer);
        this.prefsTimer = setTimeout(async () => {
            const saved = await this.orm.call("trading.chart.drawing", "save_chart_prefs", [{ ...this.state.prefs }]);
            Object.assign(this.state.prefs, saved);
        }, 500);
    }

    setMarkerSize(ev) {
        this.setPref("marker_size", parseInt(ev.target.value, 10) || 1);
    }

    async setInterval(interval) {
        if (interval === this.state.interval) {
            return;
        }
        this.cancelPending();
        this.state.interval = interval;
        storeSet(`interval:${this.instrumentId}`, interval);
        await this.load();
    }

    async onClick(param) {
        const tool = this.state.tool;
        if (tool === "cursor" || !this.instrumentId) {
            return;
        }
        const pt = this.pointOf(param);
        if (!pt) {
            return;
        }
        if (tool === "erase") {
            return this.eraseAt(pt);
        }
        if (tool === "hline") {
            return this.save({ kind: "hline", time1: pt.time, price1: pt.price, text: this.state.noteText.trim() });
        }
        if (tool === "text") {
            return this.save({ kind: "text", time1: pt.time, price1: pt.price, text: this.state.noteText.trim() || "Nota" });
        }
        if (!this.pending) {
            this.pending = { kind: tool, time1: pt.time, price1: pt.price, color: this.state.color };
            this.state.pending = true;
            return;
        }
        const first = this.pending;
        this.cancelPending();
        await this.save({ kind: tool, time1: first.time1, price1: first.price1, time2: pt.time, price2: pt.price });
    }

    async save(vals) {
        const drawing = await this.orm.call("trading.chart.drawing", "chart_create", [{
            ...vals, instrument_id: this.instrumentId, color: this.state.color,
        }]);
        this.drawings.push(drawing);
        this.state.count = this.drawings.length;
        if (drawing.kind === "hline") {
            this.renderHLines();
        }
        this.primitive.requestUpdate();
    }

    hitTest(pt) {
        const tolerance = 6;
        let best = null;
        let bestDist = tolerance;
        for (const d of this.drawings) {
            let dist = Infinity;
            if (d.kind === "hline") {
                const y = this.series.priceToCoordinate(d.price1);
                dist = y === null ? Infinity : Math.abs(pt.y - y);
            } else {
                const p1 = this.toXY(d.time1, d.price1);
                if (!p1) {
                    continue;
                }
                if (d.kind === "text") {
                    const inside = pt.x >= p1.x - 4 && pt.x <= p1.x + 8 * Math.max(4, (d.text || "Nota").length)
                        && pt.y >= p1.y - 14 && pt.y <= p1.y + 4;
                    dist = inside ? 0 : Infinity;
                } else {
                    const p2 = this.toXY(d.time2, d.price2);
                    if (!p2) {
                        continue;
                    }
                    if (d.kind === "trend") {
                        dist = distToSegment(pt.x, pt.y, p1.x, p1.y, p2.x, p2.y);
                    } else {
                        const inside = pt.x >= Math.min(p1.x, p2.x) && pt.x <= Math.max(p1.x, p2.x)
                            && pt.y >= Math.min(p1.y, p2.y) && pt.y <= Math.max(p1.y, p2.y);
                        dist = inside ? 0 : Infinity;
                    }
                }
            }
            if (dist <= bestDist) {
                best = d;
                bestDist = dist;
            }
        }
        return best;
    }

    async eraseAt(pt) {
        const target = this.hitTest(pt);
        if (!target) {
            return;
        }
        await this.orm.unlink("trading.chart.drawing", [target.id]);
        this.drawings = this.drawings.filter((d) => d.id !== target.id);
        this.state.count = this.drawings.length;
        if (target.kind === "hline") {
            this.renderHLines();
        }
        this.primitive.requestUpdate();
    }

    clearAll() {
        if (!this.drawings.length) {
            return;
        }
        this.dialog.add(ConfirmationDialog, {
            title: "Borrar dibujos",
            body: `Se borrarán los ${this.drawings.length} dibujos de este instrumento en todos los intervalos.`,
            confirmLabel: "Borrar",
            confirm: async () => {
                await this.orm.unlink("trading.chart.drawing", this.drawings.map((d) => d.id));
                this.drawings = [];
                this.state.count = 0;
                this.renderHLines();
                this.primitive.requestUpdate();
            },
            cancel: () => {},
        });
    }

    reload() {
        this.load();
    }
}

registry.category("view_widgets").add("trading_chart", {
    component: TradingChart,
});
