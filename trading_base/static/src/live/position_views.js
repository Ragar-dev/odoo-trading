import { registry } from "@web/core/registry";
import { listView } from "@web/views/list/list_view";
import { ListController } from "@web/views/list/list_controller";
import { formView } from "@web/views/form/form_view";
import { FormController } from "@web/views/form/form_controller";
import { useTradingLive } from "./trading_live_service";

/** Lista de posiciones que se recarga sola con los precios y cambios de cartera en directo. */
export class TradingPositionListController extends ListController {
    setup() {
        super.setup();
        useTradingLive(() => this.liveReload(), 2000);
    }

    async liveReload() {
        const root = this.model.root;
        if (document.hidden || root.editedRecord || root.selection?.length) {
            return;
        }
        await root.load();
    }
}

registry.category("views").add("trading_position_list", {
    ...listView,
    Controller: TradingPositionListController,
});

/** Ficha de posición: se recarga si llega un precio de su instrumento o cambia la cartera. */
export class TradingPositionFormController extends FormController {
    setup() {
        super.setup();
        useTradingLive((kind, payload) => this.liveReload(kind, payload), 1000);
    }

    async liveReload(kind, payload) {
        const record = this.model.root;
        if (document.hidden || !record.resId) {
            return;
        }
        if (kind === "prices") {
            const instrument = record.data.instrument_id;
            const instrumentId = instrument && (instrument.id ?? instrument[0]);
            if (!payload.instruments?.some((i) => i.id === instrumentId)) {
                return;
            }
        }
        if (await record.isDirty()) {
            return;
        }
        await record.load();
    }
}

registry.category("views").add("trading_position_form", {
    ...formView,
    Controller: TradingPositionFormController,
});
