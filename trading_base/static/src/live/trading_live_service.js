import { EventBus, onWillUnmount } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

/**
 * Avisos en directo del servidor (bus de Odoo):
 *  - "prices": precios nuevos ({instruments: [{id, last_price, bid, ask}]}).
 *  - "positions": la cartera de una cuenta ha cambiado ({account_id}).
 */
export const tradingLiveService = {
    dependencies: ["bus_service"],
    start(env, { bus_service }) {
        const bus = new EventBus();
        bus_service.subscribe("trading_prices", (payload) => bus.trigger("prices", payload));
        bus_service.subscribe("trading_positions", (payload) => bus.trigger("positions", payload));
        bus_service.start();
        return { bus };
    },
};

registry.category("services").add("trading_live", tradingLiveService);

/**
 * Llama a `callback(kind, payload)` con cada aviso, como mucho una vez cada `wait` ms por tipo
 * de aviso (el último del intervalo se entrega al terminarlo).
 */
export function useTradingLive(callback, wait = 1000) {
    const live = useService("trading_live");
    const listeners = {};
    const timers = {};
    for (const kind of ["prices", "positions"]) {
        let last = 0;
        let pending = null;
        const fire = () => {
            timers[kind] = null;
            last = Date.now();
            const payload = pending;
            pending = null;
            callback(kind, payload);
        };
        listeners[kind] = ({ detail }) => {
            if (kind === "prices" && pending) {
                // Se juntan los precios del intervalo: ninguno se pierde por la espera.
                const byId = new Map((pending.instruments || []).map((i) => [i.id, i]));
                for (const inst of detail.instruments || []) {
                    byId.set(inst.id, inst);
                }
                pending = { instruments: [...byId.values()] };
            } else {
                pending = detail;
            }
            if (!timers[kind]) {
                timers[kind] = setTimeout(fire, Math.max(0, wait - (Date.now() - last)));
            }
        };
        live.bus.addEventListener(kind, listeners[kind]);
    }
    onWillUnmount(() => {
        for (const kind in listeners) {
            clearTimeout(timers[kind]);
            live.bus.removeEventListener(kind, listeners[kind]);
        }
    });
}
