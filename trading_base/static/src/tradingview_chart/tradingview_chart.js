import { Component } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { cookie } from "@web/core/browser/cookie";
import { standardFieldProps } from "@web/views/fields/standard_field_props";

/**
 * Gráfica avanzada de TradingView incrustada (iframe del widget público,
 * sin clave ni librería de pago). El valor del campo es el símbolo de TradingView.
 */
export class TradingViewChart extends Component {
    static template = "trading_base.TradingViewChart";
    static props = { ...standardFieldProps };

    get symbol() {
        return this.props.record.data[this.props.name] || "";
    }

    get src() {
        const params = new URLSearchParams({
            frameElementId: `tv_${this.props.record.resId || "new"}`,
            symbol: this.symbol,
            interval: "D",
            theme: cookie.get("color_scheme") === "dark" ? "dark" : "light",
            style: "1",
            locale: "es",
            timezone: "Atlantic/Canary",
            withdateranges: "1",
            hide_side_toolbar: "0",
            allow_symbol_change: "1",
            saveimage: "0",
            studies: "[]",
        });
        return `https://s.tradingview.com/widgetembed/?${params.toString()}`;
    }

    get externalUrl() {
        return `https://es.tradingview.com/chart/?symbol=${encodeURIComponent(this.symbol)}`;
    }
}

registry.category("fields").add("tradingview_chart", {
    component: TradingViewChart,
    supportedTypes: ["char"],
});
