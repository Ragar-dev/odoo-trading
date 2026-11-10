import { Component, useState, onWillStart, onMounted, onWillUnmount } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { useTradingLive } from "../live/trading_live_service";

export class LiveBoard extends Component {
    static template = "trading_base.LiveBoard";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.state = useState({
            data: { accounts: [], instruments: [], positions: [], refresh_seconds: 5 },
            countdown: 0,
            paused: false,
            loading: false,
            error: "",
            flash: {},
        });
        this.lastPrices = {};
        onWillStart(() => this.load());
        onMounted(() => {
            this.timer = setInterval(() => this.tick(), 1000);
        });
        onWillUnmount(() => clearInterval(this.timer));
        // Con el directo activo, cada precio o cambio de cartera recarga el panel (como mucho una vez por segundo).
        useTradingLive(() => {
            if (!this.state.paused && !this.state.loading && !document.hidden) {
                this.load();
            }
        }, 1000);
    }

    get isStreaming() {
        return this.state.data.accounts.some((a) => a.stream_live);
    }

    async tick() {
        if (this.state.paused || document.hidden || this.state.loading) {
            return;
        }
        this.state.countdown -= 1;
        if (this.state.countdown <= 0) {
            await this.load();
        }
    }

    async load() {
        this.state.loading = true;
        try {
            const data = await this.orm.call("trading.instrument", "get_live_board", []);
            const flash = {};
            for (const inst of data.instruments) {
                const old = this.lastPrices[inst.id];
                if (old !== undefined && old !== inst.last_price) {
                    flash[inst.id] = inst.last_price > old ? "up" : "down";
                }
                this.lastPrices[inst.id] = inst.last_price;
            }
            this.state.flash = flash;
            this.state.data = data;
            this.state.error = "";
        } catch (e) {
            this.state.error = e?.data?.message || e?.message || String(e);
        } finally {
            this.state.loading = false;
            this.state.countdown = this.state.data.refresh_seconds || 5;
        }
    }

    togglePause() {
        this.state.paused = !this.state.paused;
    }

    refreshNow() {
        this.state.countdown = 0;
        return this.load();
    }

    fmt(value, digits = 2) {
        if (value === null || value === undefined || value === false) {
            return "—";
        }
        return new Intl.NumberFormat("es-ES", {
            minimumFractionDigits: digits,
            maximumFractionDigits: digits,
        }).format(value);
    }

    priceDigits(value) {
        return Math.abs(value) < 10 ? 4 : 2;
    }

    signClass(value) {
        return value > 0 ? "text-success" : value < 0 ? "text-danger" : "text-muted";
    }

    openRecord(model, id) {
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: model,
            res_id: id,
            views: [[false, "form"]],
            target: "current",
        });
    }

    get totals() {
        const positions = this.state.data.positions;
        return {
            invested: positions.reduce((s, p) => s + (p.amount || 0), 0),
            pnl: positions.reduce((s, p) => s + (p.unrealized_pnl || 0), 0),
        };
    }
}

registry.category("actions").add("trading_base.LiveBoard", LiveBoard);
