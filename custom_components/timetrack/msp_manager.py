"""MSP Manager REST API client for pushing time entries."""

import logging
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import aiohttp

_LOGGER = logging.getLogger(__name__)


class MSPManagerClient:
    """Client for N-able MSP Manager REST API.

    Auth: MSP Manager API keys bypass 2FA.
    The API key is used as the password in Basic Auth (username is the key name
    or left blank), or as a Bearer token — we try both patterns.
    """

    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create an aiohttp session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={
                    "X-API-Key": self.api_key,
                    "Content-Type": "application/json;odata.metadata=minimal;odata.streaming=true",
                    "Accept": "application/json;odata.metadata=minimal;odata.streaming=true",
                    "User-Agent": "TimeTrack-HA/1.0 (HomeAssistant Integration)",
                },
            )
        return self._session

    async def close(self) -> None:
        """Close the session."""
        if self._session and not self._session.closed:
            await self._session.close()

    @property
    def is_configured(self) -> bool:
        """Check if MSP Manager credentials are configured."""
        return bool(self.base_url and self.api_key)

    async def test_connection(self) -> dict:
        """Test the API connection. Returns status info."""
        if not self.is_configured:
            return {"ok": False, "error": "Not configured"}

        try:
            session = await self._get_session()
            async with session.get(
                f"{self.base_url}/Tickets?$top=1"
            ) as resp:
                if resp.status == 200:
                    _LOGGER.info("MSP Manager API connection successful")
                    return {"ok": True}
                else:
                    body = await resp.text()
                    return {
                        "ok": False,
                        "error": f"HTTP {resp.status}: {body[:200]}",
                    }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    async def fetch_tickets(self, active_only: bool = True, top: int = 200) -> list:
        """Fetch tickets from MSP Manager.

        Args:
            active_only: If True, only fetch active tickets (TicketStatusCode 5).
            top: Maximum number of tickets to return.
        """
        session = await self._get_session()
        params = {
            "$top": str(top),
            "$orderby": "CreatedDate desc",
            "$select": "TicketId,TicketNumber,Title,TicketStatusCode,"
                       "ServiceItemId,CreatedDate,CompletedDate",
        }
        if active_only:
            # Exclude only Completed(7) and Cancelled(9)
            params["$filter"] = "TicketStatusCode ne 7 and TicketStatusCode ne 9"

        try:
            async with session.get(
                f"{self.base_url}/Tickets",
                params=params,
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    tickets = data.get("value", [])
                    _LOGGER.info("Fetched %d tickets from MSP Manager", len(tickets))
                    return tickets
                else:
                    body = await resp.text()
                    _LOGGER.error("Failed to fetch tickets: HTTP %d — %s", resp.status, body[:200])
                    return []
        except Exception as exc:
            _LOGGER.error("Error fetching tickets: %s", exc)
            return []

    async def fetch_service_item_rates(self) -> list:
        """Fetch service-item rates from MSP Manager.

        Uses /serviceitemratesview to get current rate UUIDs.
        Each customer ServiceItem has its own set of rate IDs.
        """
        session = await self._get_session()
        try:
            async with session.get(
                f"{self.base_url}/serviceitemratesview",
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    # Response can be a list or {value: [...]}
                    rates = data if isinstance(data, list) else data.get("value", [])
                    _LOGGER.info("Fetched %d service item rates from MSP Manager", len(rates))
                    return rates
                else:
                    body = await resp.text()
                    _LOGGER.error("Failed to fetch rates: HTTP %d — %s", resp.status, body[:200])
                    return []
        except Exception as exc:
            _LOGGER.error("Error fetching service item rates: %s", exc)
            return []

    async def fetch_customers(self) -> list:
        """Fetch customers from MSP Manager.

        Returns list of customer objects with CustomerId, CustomerName, ShortName, etc.
        """
        session = await self._get_session()
        try:
            async with session.get(
                f"{self.base_url}/customers",
                params={"$select": "CustomerId,CustomerName,CustomerStatusId"},
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    customers = data if isinstance(data, list) else data.get("value", [])
                    _LOGGER.info("Fetched %d customers from MSP Manager", len(customers))
                    return customers
                else:
                    body = await resp.text()
                    _LOGGER.error("Failed to fetch customers: HTTP %d — %s", resp.status, body[:200])
                    return []
        except Exception as exc:
            _LOGGER.error("Error fetching customers: %s", exc)
            return []

    async def create_time_entry(
        self,
        ticket_id: str,
        service_item_rate_id: str,
        start_time: datetime,
        end_time: datetime,
        rounded_hours: float,
        description: str = "",
    ) -> Optional[dict]:
        """Create a time entry on an MSP Manager ticket.

        Uses POST /tickettimeentries with the TicketTimeEntryRequest schema:
        - startTime (required): ISO 8601 datetime
        - endTime (required): ISO 8601 datetime
        - ticketId (required): UUID of the ticket
        - serviceItemRateId (required): UUID of the service item/rate
        - description (optional): note for the time entry
        - timeEntryTypeCode (optional): integer, defaults to 0
        """
        if not self.is_configured:
            _LOGGER.warning("MSP Manager not configured — skipping time entry push")
            return None

        # Convert naive local datetimes to UTC
        # DB stores naive local times; MSP Manager API expects UTC
        def _to_utc_iso(dt: datetime, local_tz: str = "America/Denver") -> str:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=ZoneInfo(local_tz))
            return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        payload = {
            "startTime": _to_utc_iso(start_time),
            "endTime": _to_utc_iso(end_time),
            "ticketId": ticket_id,
            "serviceItemRateId": service_item_rate_id,
            "description": description or f"Auto-logged by TimeTrack ({rounded_hours:.2f}h)",
            "timeEntryTypeCode": 0,
        }

        _LOGGER.info(
            "Pushing time entry to MSP Manager: ticket=%s, %.2fh",
            ticket_id,
            rounded_hours,
        )

        try:
            session = await self._get_session()
            async with session.post(
                f"{self.base_url}/tickettimeentries",
                json=payload,
            ) as resp:
                if resp.status in (200, 201):
                    result = await resp.json()
                    entry_id = result.get("ticketTimeEntryId")
                    _LOGGER.info(
                        "✅ Time entry created on ticket %s: %.2fh (id=%s)",
                        ticket_id, rounded_hours, entry_id,
                    )

                    # PUT the same payload to trigger TimeRounded calculation.
                    # MSP Manager's API doesn't compute rounded time on POST —
                    # a follow-up PUT acts like a "save" and triggers it.
                    if entry_id:
                        async with session.put(
                            f"{self.base_url}/tickettimeentries/{entry_id}",
                            json=payload,
                        ) as put_resp:
                            if put_resp.status == 200:
                                _LOGGER.info("✅ Time entry saved (TimeRounded triggered)")
                            else:
                                put_body = await put_resp.text()
                                _LOGGER.warning(
                                    "⚠️ PUT save failed (entry exists but may show 0h): HTTP %d — %s",
                                    put_resp.status, put_body[:200],
                                )

                    return result
                else:
                    body = await resp.text()
                    _LOGGER.error(
                        "❌ MSP Manager time entry failed: HTTP %d — %s",
                        resp.status,
                        body[:500],
                    )
                    return None
        except Exception as exc:
            _LOGGER.error("❌ MSP Manager API error: %s", exc)
            return None

    async def create_ticket(
        self,
        title: str,
        service_item_id: str,
        description: str = "",
        priority: int = 2,
        is_billable: bool = True,
    ) -> Optional[dict]:
        """Create a new ticket in MSP Manager.

        Args:
            title: Ticket title (e.g. "Monthly Onsite - March 2026")
            service_item_id: UUID of the ServiceItem (determines customer)
            description: Optional description
            priority: Priority code (1=Low, 2=Normal, 3=High, 4=Critical)
            is_billable: Whether time entries are billable
        Returns:
            Ticket dict from API response, or None on failure.
        """
        if not self.is_configured:
            _LOGGER.warning("MSP Manager not configured — cannot create ticket")
            return None

        payload = {
            "Title": title,
            "ServiceItemId": service_item_id,
            "Description": description,
            "TicketPriorityCode": priority,
            "IsBillable": is_billable,
            "IsTaxable": False,
        }

        _LOGGER.info("Creating ticket in MSP Manager: %s", title)

        try:
            session = await self._get_session()
            async with session.post(
                f"{self.base_url}/Tickets",
                json=payload,
            ) as resp:
                if resp.status in (200, 201):
                    result = await resp.json()
                    _LOGGER.info(
                        "✅ Ticket created: %s (ID: %s)",
                        title,
                        result.get("TicketId", "?"),
                    )
                    return result
                else:
                    body = await resp.text()
                    _LOGGER.error(
                        "❌ Ticket creation failed: HTTP %d — %s",
                        resp.status,
                        body[:500],
                    )
                    return None
        except Exception as exc:
            _LOGGER.error("❌ Ticket creation error: %s", exc)
            return None

    async def get_tickets(self, filter_str: str = "") -> list[dict]:
        """Get tickets, optionally filtered."""
        if not self.is_configured:
            return []

        url = f"{self.base_url}/Tickets"
        if filter_str:
            url += f"?{filter_str}"

        try:
            session = await self._get_session()
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("value", [])
                else:
                    _LOGGER.error("Failed to get tickets: HTTP %d", resp.status)
                    return []
        except Exception as exc:
            _LOGGER.error("Error getting tickets: %s", exc)
            return []
