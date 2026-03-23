"""TimeTrack - Automatic time tracking via HA zones with MSP Manager integration."""

import logging
from datetime import datetime, timedelta
from pathlib import Path

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, Event, callback
from homeassistant.helpers.event import async_track_time_change
import voluptuous as vol
from homeassistant.helpers import config_validation as cv

from .const import (
    DOMAIN,
    PLATFORMS,
    CONF_PERSON_ENTITY,
    CONF_MSP_URL,
    CONF_MSP_API_KEY,
    CONF_MSP_DRY_RUN,
    CONF_ROUNDING_MINUTES,
    CONF_MIN_SESSION_MINUTES,
    DEFAULT_PERSON_ENTITY,
    DEFAULT_ROUNDING_MINUTES,
    DEFAULT_MIN_SESSION_MINUTES,
    DEFAULT_MSP_URL,
    DB_FILE,
)
from .store import TimeTrackStore
from .tracker import ZoneTracker
from .msp_manager import MSPManagerClient

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up TimeTrack from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    # Initialize the data store
    db_path = Path(hass.config.path(DB_FILE))
    recorder_db_path = str(Path(hass.config.path("home-assistant_v2.db")))
    store = await hass.async_add_executor_job(
        TimeTrackStore, str(db_path), recorder_db_path
    )

    # Initialize the zone tracker
    person_entity = entry.data.get(CONF_PERSON_ENTITY, DEFAULT_PERSON_ENTITY)
    rounding = entry.data.get(CONF_ROUNDING_MINUTES, DEFAULT_ROUNDING_MINUTES)
    min_session = entry.data.get(CONF_MIN_SESSION_MINUTES, DEFAULT_MIN_SESSION_MINUTES)

    tracker = ZoneTracker(
        hass=hass,
        store=store,
        person_entity=person_entity,
        min_session_minutes=min_session,
        rounding_minutes=rounding,
    )

    # Initialize MSP Manager client
    msp_url = entry.data.get(CONF_MSP_URL, DEFAULT_MSP_URL)
    msp_api_key = entry.data.get(CONF_MSP_API_KEY, "")
    msp_dry_run = entry.data.get(CONF_MSP_DRY_RUN, True)
    msp_client = MSPManagerClient(msp_url, msp_api_key)

    if msp_client.is_configured:
        if msp_dry_run:
            _LOGGER.info("MSP Manager configured in DRY RUN mode — will log but NOT push")
        else:
            _LOGGER.info("MSP Manager configured — time entries will auto-push to tickets")
    else:
        _LOGGER.info("MSP Manager not configured — time entries stored locally only")

    # Resolve authorized user from person entity
    authorized_user_id = None
    person_state = hass.states.get(person_entity)
    if person_state:
        authorized_user_id = person_state.attributes.get("user_id")
        if authorized_user_id:
            _LOGGER.info("Auth guard: services restricted to user %s (%s)", person_entity, authorized_user_id)
        else:
            _LOGGER.warning("Auth guard: person %s has no user_id — auth guard disabled", person_entity)
    else:
        _LOGGER.warning("Auth guard: person entity %s not found — auth guard disabled", person_entity)

    # Store references
    hass.data[DOMAIN][entry.entry_id] = {
        "store": store,
        "tracker": tracker,
        "msp_client": msp_client,
        "authorized_user_id": authorized_user_id,
    }

    # Listen for clock-out events — tentative ticket assignment (NO auto-push)
    @callback
    def _handle_clock_out_event(event: Event) -> None:
        """Assign tentative ticket on clock-out (batch push later)."""
        entry_id = event.data.get("entry_id")
        client_name = event.data.get("client")

        # Look up default ticket for this client
        client_info = store.get_client_by_zone(event.data.get("zone", ""))
        if client_info and client_info.get("msp_ticket_id"):
            # Tentatively assign the default ticket
            store.update_entry(
                entry_id, msp_ticket_id=client_info["msp_ticket_id"]
            )
            _LOGGER.info(
                "📋 Entry %d for %s tentatively assigned to ticket %s",
                entry_id, client_name, client_info["msp_ticket_id"],
            )
        else:
            _LOGGER.info(
                "📋 Entry %d for %s stored (no default ticket mapped)",
                entry_id, client_name,
            )

    hass.bus.async_listen("timetrack_clock_out", _handle_clock_out_event)

    # Start tracking
    await tracker.async_start()

    # Set up platforms (sensors)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # ── Auto-register Lovelace card ──
    card_path = Path(__file__).parent / "timetrack-card.js"
    if card_path.exists():
        card_url = f"/{DOMAIN}/timetrack-card.js"
        from homeassistant.components.http import StaticPathConfig
        await hass.http.async_register_static_paths(
            [StaticPathConfig(card_url, str(card_path), False)]
        )

        # Register as a Lovelace resource so the card auto-loads on all dashboards
        try:
            lovelace = hass.data["lovelace"]
            resources = lovelace.resources
            # Check if already registered (idempotent)
            existing = [
                r for r in resources.async_items()
                if r.get("url", "").startswith(card_url)
            ]
            if not existing:
                await resources.async_create_item(
                    {"res_type": "module", "url": card_url}
                )
                _LOGGER.info("Registered Lovelace card resource: %s", card_url)
            else:
                _LOGGER.debug("Lovelace card resource already registered")
        except Exception as exc:
            _LOGGER.warning("Could not auto-register card resource: %s", exc)
    else:
        _LOGGER.debug("Card JS not found at %s — skipping", card_path)

    # Register services
    _register_services(hass, store, tracker, msp_client, rounding, msp_dry_run, authorized_user_id, person_entity)

    # ── Nightly auto-generate from history (1:00 AM) ──

    async def _nightly_generate(_now):
        """Generate yesterday's entries from zone history."""
        yesterday = datetime.now() - timedelta(days=1)
        start_date = yesterday.strftime("%Y-%m-%d")
        end_date = start_date

        _LOGGER.info("🌙 Nightly auto-generate for %s", start_date)

        def _get_mid():
            import sqlite3
            rconn = sqlite3.connect(recorder_db_path)
            rconn.row_factory = sqlite3.Row
            row = rconn.execute(
                "SELECT metadata_id FROM states_meta WHERE entity_id = ?",
                (person_entity,),
            ).fetchone()
            rconn.close()
            return row["metadata_id"] if row else None

        mid = await hass.async_add_executor_job(_get_mid)
        if not mid:
            _LOGGER.error("Nightly generate: no metadata_id for %s", person_entity)
            return

        result = await hass.async_add_executor_job(
            store.generate_entries_from_history,
            start_date, end_date, mid, rounding,
        )
        _LOGGER.info(
            "🌙 Nightly: generated %d entries (%d skipped) for %s",
            result["generated"], result["skipped"], start_date,
        )
        hass.bus.async_fire("timetrack_entries_generated", result)

    try:
        unsub_nightly = async_track_time_change(hass, _nightly_generate, hour=1, minute=0, second=0)
        hass.data[DOMAIN][entry.entry_id]["unsub_nightly"] = unsub_nightly
        _LOGGER.info("TimeTrack integration loaded — tracking %s (nightly generate at 1:00 AM)", person_entity)
    except Exception as exc:
        _LOGGER.warning("Nightly scheduler setup failed (non-fatal): %s", exc)
        _LOGGER.info("TimeTrack integration loaded — tracking %s (nightly generate DISABLED)", person_entity)
    return True


async def _push_single_entry(
    hass: HomeAssistant,
    msp_client: MSPManagerClient,
    store: TimeTrackStore,
    entry: dict,
    dry_run: bool = True,
) -> bool:
    """Push a single pending entry to MSP Manager."""
    entry_id = entry["id"]
    ticket_id = entry.get("resolved_ticket_id") or entry.get("msp_ticket_id")
    rate_id = entry.get("resolved_rate_id") or entry.get("msp_service_item_rate_id") or store.get_default_rate_id()
    client_name = entry["client_name"]
    # Description priority: entry description > client default > generic fallback
    client_info = await hass.async_add_executor_job(
        store.get_client_by_name, client_name
    )
    client_default = (client_info or {}).get("default_description", "") if client_info else ""
    description = entry.get("description") or client_default or f"TimeTrack: {client_name}"
    clock_in = datetime.fromisoformat(entry["clock_in"])
    clock_out = datetime.fromisoformat(entry["clock_out"])
    rounded_hours = entry.get("rounded_hours", 0)

    if not ticket_id:
        _LOGGER.warning("Entry %d has no ticket — cannot push", entry_id)
        return False

    if dry_run:
        _LOGGER.info(
            "🔸 DRY RUN push_entries — entry #%d\n"
            "  client:      %s\n"
            "  ticket_id:   %s\n"
            "  rate_id:     %s\n"
            "  start:       %s\n"
            "  end:         %s\n"
            "  hours:       %.2f (rounded)\n"
            "  description: %s",
            entry_id, client_name, ticket_id, rate_id,
            clock_in.strftime("%Y-%m-%d %H:%M"),
            clock_out.strftime("%Y-%m-%d %H:%M"),
            rounded_hours, description,
        )
        return True

    result = await msp_client.create_time_entry(
        ticket_id=ticket_id,
        service_item_rate_id=rate_id,
        start_time=clock_in,
        end_time=clock_out,
        rounded_hours=rounded_hours,
        description=description,
    )
    if result:
        await hass.async_add_executor_job(store.mark_pushed, entry_id)
        _LOGGER.info(
            "✅ Pushed %.2fh to ticket %s for %s",
            rounded_hours, ticket_id, client_name,
        )
        return True
    else:
        await hass.async_add_executor_job(store.mark_push_failed, entry_id)
        _LOGGER.error("❌ Failed to push entry %d", entry_id)
        return False


def _check_auth(
    call: ServiceCall,
    authorized_user_id: str | None,
) -> bool:
    """Check if the service call is from the authorized user.

    Returns True if authorized, False if rejected.
    If authorized_user_id is None (not resolved), allow all calls.
    """
    if not authorized_user_id:
        return True  # Guard disabled — person entity had no user_id
    caller_id = call.context.user_id
    if caller_id is None:
        return True  # Automation / internal call — allow
    if caller_id != authorized_user_id:
        _LOGGER.warning(
            "🔒 Auth rejected: user %s attempted %s (authorized: %s)",
            caller_id, call.service, authorized_user_id,
        )
        return False
    return True


def _register_services(
    hass: HomeAssistant,
    store: TimeTrackStore,
    tracker: ZoneTracker,
    msp_client: MSPManagerClient,
    rounding_minutes: int,
    dry_run: bool = True,
    authorized_user_id: str | None = None,
    person_entity: str = "",
) -> None:
    """Register TimeTrack services."""

    # Guard: don't re-register services if already registered (reload safety)
    if hass.services.has_service(DOMAIN, "clock_in"):
        _LOGGER.debug("Services already registered, skipping")
        return

    async def handle_clock_in(call: ServiceCall) -> None:
        if not _check_auth(call, authorized_user_id):
            return
        client = call.data.get("client")
        zone = call.data.get("zone", "")
        await hass.async_add_executor_job(tracker.manual_clock_in, client, zone)

    async def handle_clock_out(call: ServiceCall) -> None:
        if not _check_auth(call, authorized_user_id):
            return
        await hass.async_add_executor_job(tracker.manual_clock_out)

    async def handle_report(call: ServiceCall) -> dict:
        from datetime import date

        year = call.data.get("year", date.today().year)
        month = call.data.get("month", date.today().month)
        report = await hass.async_add_executor_job(
            store.generate_report, year, month, rounding_minutes
        )
        hass.bus.async_fire(
            "timetrack_report_generated",
            {"report": report, "year": year, "month": month},
        )
        return {"report": report}

    async def handle_map_client(call: ServiceCall) -> None:
        """Map a client zone to an MSP Manager ticket."""
        if not _check_auth(call, authorized_user_id):
            return
        client = call.data.get("client")
        ticket_id = call.data.get("ticket_id")
        service_item_rate_id = call.data.get("service_item_rate_id", "")
        msp_client_name = call.data.get("msp_client_name", "")
        default_description = call.data.get("default_description", "")
        zone = call.data.get("zone", f"TimeTrack - {client}")
        await hass.async_add_executor_job(
            store.add_client, client, zone, ticket_id,
            service_item_rate_id, msp_client_name, default_description,
        )
        _LOGGER.info("Mapped client %s → MSP ticket %s", client, ticket_id)

    async def handle_push_entries(call: ServiceCall) -> None:
        """Batch push pending entries to MSP Manager."""
        if not _check_auth(call, authorized_user_id):
            return
        entry_ids = call.data.get("entry_ids", [])  # Empty = push all pending
        pending = await hass.async_add_executor_job(store.get_pending_entries)

        if entry_ids:
            pending = [e for e in pending if e["id"] in entry_ids]

        if not pending:
            _LOGGER.info("No pending entries to push")
            return

        pushed = 0
        failed = 0
        for entry in pending:
            success = await _push_single_entry(
                hass, msp_client, store, entry, dry_run=dry_run
            )
            if success:
                pushed += 1
            else:
                failed += 1

        _LOGGER.info(
            "📤 Batch push complete: %d pushed, %d failed, %d total",
            pushed, failed, len(pending),
        )
        hass.bus.async_fire(
            "timetrack_push_complete",
            {"pushed": pushed, "failed": failed, "total": len(pending)},
        )

    async def handle_edit_entry(call: ServiceCall) -> None:
        """Edit an entry's description or ticket assignment."""
        if not _check_auth(call, authorized_user_id):
            return
        entry_id = call.data.get("entry_id")
        description = call.data.get("description")
        ticket_id = call.data.get("ticket_id")
        billable = call.data.get("billable")

        # Validate ticket-client association
        if ticket_id:
            ticket = await hass.async_add_executor_job(
                store.get_ticket_by_id, ticket_id
            )
            if ticket:
                entry = await hass.async_add_executor_job(
                    store.get_entry_by_id, entry_id
                )
                if entry and ticket.get("customer_short") and entry["client_name"] != ticket["customer_short"]:
                    _LOGGER.warning(
                        "⚠️ Ticket #%s (%s) does not match entry client %s — rejected",
                        ticket.get("ticket_number"), ticket.get("customer_short"),
                        entry["client_name"],
                    )
                    return

        await hass.async_add_executor_job(
            store.update_entry, entry_id, description, ticket_id, billable
        )
        _LOGGER.info("Updated entry %d", entry_id)

    async def handle_sync_tickets(call: ServiceCall) -> None:
        """Sync active tickets from MSP Manager."""
        if not msp_client.is_configured:
            _LOGGER.warning("MSP Manager not configured — cannot sync tickets")
            return
        service_items = await msp_client.fetch_service_items()
        tickets = await msp_client.fetch_tickets(active_only=True)
        if tickets:
            count = await hass.async_add_executor_job(
                store.upsert_tickets, tickets, service_items
            )
            _LOGGER.info("🔄 Synced %d active tickets from MSP Manager", count)
            hass.bus.async_fire("timetrack_tickets_synced", {"count": count})
        else:
            _LOGGER.info("No active tickets returned from MSP Manager")

    # Register all services
    hass.services.async_register(
        DOMAIN, "clock_in", handle_clock_in,
        schema=vol.Schema({
            vol.Required("client"): cv.string,
            vol.Optional("zone", default=""): cv.string,
        }),
    )

    hass.services.async_register(
        DOMAIN, "clock_out", handle_clock_out,
        schema=vol.Schema({}),
    )

    hass.services.async_register(
        DOMAIN, "report", handle_report,
        schema=vol.Schema({
            vol.Optional("year"): cv.positive_int,
            vol.Optional("month"): cv.positive_int,
        }),
    )

    hass.services.async_register(
        DOMAIN, "map_client", handle_map_client,
        schema=vol.Schema({
            vol.Required("client"): cv.string,
            vol.Required("ticket_id"): cv.string,
            vol.Optional("service_item_rate_id", default=""): cv.string,
            vol.Optional("msp_client_name", default=""): cv.string,
            vol.Optional("zone", default=""): cv.string,
            vol.Optional("default_description", default=""): cv.string,
        }),
    )

    hass.services.async_register(
        DOMAIN, "push_entries", handle_push_entries,
        schema=vol.Schema({
            vol.Optional("entry_ids", default=[]): [cv.positive_int],
        }),
    )

    hass.services.async_register(
        DOMAIN, "edit_entry", handle_edit_entry,
        schema=vol.Schema({
            vol.Required("entry_id"): cv.positive_int,
            vol.Optional("description"): cv.string,
            vol.Optional("ticket_id"): cv.string,
            vol.Optional("billable"): cv.boolean,
        }),
    )

    hass.services.async_register(
        DOMAIN, "sync_tickets", handle_sync_tickets,
        schema=vol.Schema({}),
    )

    async def handle_create_ticket(call: ServiceCall) -> None:
        """Create a new ticket in MSP Manager."""
        if not _check_auth(call, authorized_user_id):
            return
        customer_short = call.data.get("customer")
        title = call.data.get("title")
        description = call.data.get("description", "")

        # Look up ServiceItemId for this customer
        svc_id = await hass.async_add_executor_job(
            store.get_service_item_for_customer, customer_short
        )
        if not svc_id:
            _LOGGER.error("No ServiceItemId found for customer '%s'", customer_short)
            return

        if not msp_client.is_configured:
            _LOGGER.warning("MSP Manager not configured — cannot create ticket")
            return

        if dry_run:
            _LOGGER.info(
                "🔸 DRY RUN create_ticket\n"
                "  customer:       %s\n"
                "  title:          %s\n"
                "  service_item:   %s\n"
                "  description:    %s",
                customer_short, title, svc_id, description or "(none)",
            )
            hass.bus.async_fire("timetrack_ticket_created", {
                "dry_run": True,
                "title": title,
                "customer": customer_short,
                "service_item_id": svc_id,
            })
            return

        result = await msp_client.create_ticket(
            title=title,
            service_item_id=svc_id,
            description=description,
        )
        if result:
            # Upsert the new ticket into local DB
            await hass.async_add_executor_job(store.upsert_tickets, [result])
            hass.bus.async_fire("timetrack_ticket_created", {
                "ticket_id": result.get("TicketId"),
                "title": title,
                "customer": customer_short,
            })
            # Auto-sync all tickets so dropdowns update immediately
            try:
                tickets = await msp_client.fetch_tickets(active_only=True)
                if tickets:
                    await hass.async_add_executor_job(store.upsert_tickets, tickets)
                    _LOGGER.info("🔄 Auto-synced tickets after creation")
            except Exception as err:
                _LOGGER.warning("Auto-sync after ticket creation failed: %s", err)

    hass.services.async_register(
        DOMAIN, "create_ticket", handle_create_ticket,
        schema=vol.Schema({
            vol.Required("customer"): cv.string,
            vol.Required("title"): cv.string,
            vol.Optional("description", default=""): cv.string,
        }),
    )

    # ── History-Based Entry Generation ──

    async def handle_generate_entries(call: ServiceCall) -> None:
        """Generate time entries from HA zone history."""
        if not _check_auth(call, authorized_user_id):
            return
        start_date = call.data.get("start_date")
        end_date = call.data.get("end_date")

        # Resolve person entity metadata_id from recorder
        import sqlite3 as _sqlite3
        recorder_path = str(Path(hass.config.path("home-assistant_v2.db")))
        person_entity_id = person_entity

        def _get_metadata_id():
            rconn = _sqlite3.connect(recorder_path)
            rconn.row_factory = _sqlite3.Row
            row = rconn.execute(
                "SELECT metadata_id FROM states_meta WHERE entity_id = ?",
                (person_entity_id,),
            ).fetchone()
            rconn.close()
            return row["metadata_id"] if row else None

        mid = await hass.async_add_executor_job(_get_metadata_id)
        if not mid:
            _LOGGER.error("Could not find metadata_id for %s", person_entity_id)
            return

        result = await hass.async_add_executor_job(
            store.generate_entries_from_history,
            start_date, end_date, mid, rounding_minutes,
        )
        _LOGGER.info(
            "📊 Generated %d entries (%d skipped, %d errors) for %s → %s",
            result["generated"], result["skipped"], result["errors"],
            start_date, end_date,
        )
        # Fire event so the card can show the result
        hass.bus.async_fire("timetrack_entries_generated", result)

    hass.services.async_register(
        DOMAIN, "generate_entries", handle_generate_entries,
        schema=vol.Schema({
            vol.Required("start_date"): cv.string,
            vol.Required("end_date"): cv.string,
        }),
    )

    # ── Zone Alias Management ──

    async def handle_add_zone_alias(call: ServiceCall) -> None:
        if not _check_auth(call, authorized_user_id):
            return
        zone = call.data["zone_state"]
        client = call.data["client_name"]
        await hass.async_add_executor_job(store.add_zone_alias, zone, client)
        _LOGGER.info("Added zone alias: %s → %s", zone, client)

    async def handle_remove_zone_alias(call: ServiceCall) -> None:
        if not _check_auth(call, authorized_user_id):
            return
        zone = call.data["zone_state"]
        await hass.async_add_executor_job(store.remove_zone_alias, zone)
        _LOGGER.info("Removed zone alias: %s", zone)

    hass.services.async_register(
        DOMAIN, "add_zone_alias", handle_add_zone_alias,
        schema=vol.Schema({
            vol.Required("zone_state"): cv.string,
            vol.Required("client_name"): cv.string,
        }),
    )
    hass.services.async_register(
        DOMAIN, "remove_zone_alias", handle_remove_zone_alias,
        schema=vol.Schema({
            vol.Required("zone_state"): cv.string,
        }),
    )

    _LOGGER.info(
        "TimeTrack services registered: clock_in, clock_out, report, "
        "map_client, push_entries, edit_entry, sync_tickets, create_ticket, "
        "generate_entries, add_zone_alias, remove_zone_alias"
    )

    # Auto-sync active tickets and rates from MSP Manager on startup
    if msp_client.is_configured:
        async def _startup_sync(*_):
            # Customers FIRST — tickets need the customer map for short names
            try:
                customers = await msp_client.fetch_customers()
                if customers:
                    count = await hass.async_add_executor_job(store.sync_customers, customers)
                    _LOGGER.info("🚀 Startup: synced %d customers from MSP Manager", count)
            except Exception as exc:
                _LOGGER.warning("Startup customer sync failed: %s", exc)
            # Service items provide ServiceItemId → CustomerId mapping
            service_items = []
            try:
                service_items = await msp_client.fetch_service_items()
            except Exception as exc:
                _LOGGER.warning("Startup service items fetch failed: %s", exc)
            try:
                tickets = await msp_client.fetch_tickets(active_only=True)
                if tickets:
                    count = await hass.async_add_executor_job(
                        store.upsert_tickets, tickets, service_items
                    )
                    _LOGGER.info("🚀 Startup: synced %d active tickets from MSP Manager", count)
            except Exception as exc:
                _LOGGER.warning("Startup ticket sync failed: %s", exc)
            try:
                rates = await msp_client.fetch_service_item_rates()
                if rates:
                    count = await hass.async_add_executor_job(store.sync_service_item_rates, rates)
                    _LOGGER.info("🚀 Startup: synced %d service item rates from MSP Manager", count)
            except Exception as exc:
                _LOGGER.warning("Startup rate sync failed: %s", exc)

        hass.bus.async_listen_once("homeassistant_started", _startup_sync)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    data = hass.data[DOMAIN].get(entry.entry_id)
    if data:
        await data["tracker"].async_stop()
        await data["msp_client"].close()
        # Cancel nightly scheduler
        unsub = data.get("unsub_nightly")
        if unsub:
            unsub()

    # Remove Lovelace card resource registration
    card_url = f"/{DOMAIN}/timetrack-card.js"
    try:
        lovelace = hass.data.get("lovelace")
        if lovelace and hasattr(lovelace, "resources"):
            resources = lovelace.resources
            for r in resources.async_items():
                if r.get("url", "").startswith(card_url):
                    await resources.async_delete_item(r["id"])
                    _LOGGER.info("Removed Lovelace card resource: %s", card_url)
                    break
    except Exception as exc:
        _LOGGER.debug("Could not remove Lovelace card resource: %s", exc)

    # Remove extra JS URL
    try:
        from homeassistant.components.frontend import remove_extra_js_url
        remove_extra_js_url(hass, card_url)
    except Exception:
        pass

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Clean up when the integration is fully removed."""
    # Delete the TimeTrack database
    db_path = Path(hass.config.path("timetrack.db"))
    if db_path.exists():
        try:
            db_path.unlink()
            _LOGGER.info("Deleted TimeTrack database: %s", db_path)
            # Also clean up WAL/SHM files if they exist
            for suffix in ("-wal", "-shm"):
                wal = db_path.with_name(db_path.name + suffix)
                if wal.exists():
                    wal.unlink()
        except Exception as exc:
            _LOGGER.warning("Could not delete TimeTrack database: %s", exc)
