# TimeTrack for Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)

**Automatic time tracking via Home Assistant zones with MSP Manager integration.**

TimeTrack automatically generates billable time entries by tracking your location through HA zones. When you arrive at a client site, TimeTrack clocks you in. When you leave, it clocks you out. Entries are created and pushed to N-able MSP Manager tickets.

## Features

- 🕐 **Automatic zone-based clock in/out** — arrival and departure detected via HA person entity
- 📊 **History-based generation** — backfill entries from HA recorder data for any past month
- 🌙 **Nightly auto-generate** — automatically creates yesterday's entries at 1:00 AM
- 🎫 **Ticket management** — create, sync, and assign MSP Manager tickets
- 📤 **Push to MSP Manager** — batch push pending entries with dry-run safety
- 🗺️ **Zone aliases** — map HA zone states to client names flexibly
- 💰 **Billable toggle** — mark entries as non-billable
- 🔒 **User auth guard** — card only visible to the tracked person

## Installation

### HACS (Recommended)

1. Open HACS in your Home Assistant
2. Click the 3-dot menu → **Custom repositories**
3. Add `https://github.com/williamhart-az/ha-timetrack` as type **Integration**
4. Click **Download**
5. Restart Home Assistant

### Lovelace Card

After installing the integration, add the card resource:

1. Copy `www/timetrack-card.js` to your HA's `www/` directory (HACS does this for you)
2. Go to **Settings → Dashboards → Resources**
3. Add `/local/timetrack-card.js` as a JavaScript Module
4. Add the card to a dashboard:
   ```yaml
   type: custom:timetrack-card
   ```

### Manual Installation

1. Copy `custom_components/timetrack/` to your HA's `custom_components/` directory
2. Copy `www/timetrack-card.js` to your HA's `www/` directory  
3. Restart Home Assistant

## Configuration

After installation, go to **Settings → Devices & Services → Add Integration → TimeTrack**.

| Option | Description | Default |
|--------|-------------|---------|
| Person entity | The `person.*` entity to track | *(required)* |
| MSP Manager API URL | OData API endpoint | `https://api.mspmanager.com/odata` |
| MSP Manager API Key | Your API key for authentication | — |
| Dry Run | Log pushes without creating entries | `true` |
| Rounding (minutes) | Round entry duration to nearest N min | `15` |
| Min session (minutes) | Ignore visits shorter than this | `15` |

## How It Works

1. **Create HA zones** named `TimeTrack - ClientName` for each client site
2. **Map clients** in the Clients tab — assign tickets and service rates
3. **Zone aliases** let you map any zone name to a client (e.g., `home` → `Internal`)
4. TimeTrack detects zone transitions and creates pending time entries
5. Review entries in the **Pending** tab, then push to MSP Manager

## Requirements

- Home Assistant 2024.1+
- N-able MSP Manager account (optional — works standalone for local time tracking)
- HACS (for easy installation)

## License

MIT
