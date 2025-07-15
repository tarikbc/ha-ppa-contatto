# PPA Contatto Home Assistant Integration

[![GitHub Release][releases-shield]][releases]
[![GitHub Activity][commits-shield]][commits]
[![Project Maintenance][maintenance-shield]][user_profile]

<img src="https://play-lh.googleusercontent.com/qDtSOerKV_rVZ2ZMi_-pFe7jccoGVH0aHDbykUAQeE15_UoWa0Ej1dKt3FfaQCh1PoI=w480-h960-rw" alt="PPA Contatto App" width="200" align="right">

A custom Home Assistant integration for PPA Contatto gate and relay controllers.

> **⚠️ Installation Note**: This integration is not yet available in the official Home Assistant Community Store (HACS) or the official Home Assistant integration list. For now, it must be installed manually. We're working on getting it officially listed!

## Features

- **Authentication**: Secure login using email and password
- **Device Discovery**: Automatically discovers all available gates and relays
- **Cover Control**: Control gates and doors through Home Assistant cover entities (open/close)
- **Enhanced Status**: Real-time status from device reports for maximum accuracy
- **Activity History**: Track who performed actions and when
- **Multiple Entity Types**: Covers for control + sensors for monitoring + switches for configuration
- **Device Information**: View device details like serial number, version, MAC address
- **Smart Updates**: Combines device polling with activity reports every 3 seconds
- **Device Configuration**: Configure device settings, names, notifications, and relay behavior
- **Relay Duration Control**: Set relay duration (momentary button) or switch mode (on/off toggle)
- **Professional Branding**: Displays official PPA Contatto logo and branding in device info

## Installation

### Manual Installation (Required for now)

Since this integration is not yet available through HACS or the official Home Assistant integration list, you'll need to install it manually:

1. Download the latest release from [GitHub Releases](https://github.com/tarikbc/ha-ppa-contatto/releases)
2. Extract the `custom_components/ppa_contatto` folder to your Home Assistant `custom_components` directory
   - Your path should look like: `config/custom_components/ppa_contatto/`
3. Restart Home Assistant
4. Go to **Settings** → **Devices & Services** → **Add Integration**
5. Search for "PPA Contatto" and select it
6. Enter your PPA Contatto email and password

### Future HACS Installation (Coming Soon)

Once this integration is accepted into HACS, you'll be able to install it through:

1. Ensure [HACS](https://hacs.xyz/) is installed
2. Go to **HACS** → **Integrations**
3. Click the **+** button and search for "PPA Contatto"
4. Install the integration
5. Restart Home Assistant
6. Go to **Settings** → **Devices & Services** → **Add Integration**
7. Search for "PPA Contatto" and configure with your credentials

## Configuration

### Through the UI

1. Navigate to **Configuration** → **Integrations**
2. Click the **+** button to add a new integration
3. Search for "PPA Contatto"
4. Enter your credentials:
   - **Email**: Your PPA Contatto account email
   - **Password**: Your PPA Contatto account password

### Manual Configuration (Not Recommended)

This integration supports configuration through the UI only. Manual YAML configuration is not supported.

## Usage

### Covers (Main Control)

The integration creates cover entities for each gate and door that is configured to be shown in your PPA Contatto account:

- **Gate covers**: Control gate opening/closing (e.g., `cover.abc12345_gate`) - Gates may stay open for extended periods
- **Door covers**: Control doors via relay (e.g., `cover.abc12345_door`) - Behavior depends on relay duration setting:
  - **Momentary mode** (positive duration): Acts like a button press, shows "opening" during activation
  - **Toggle mode** (-1 duration): Acts as on/off switch, door stays open/closed until toggled

### Configuration Switches

Additional switches for device configuration:

- **Favorite toggle**: Mark/unmark device as favorite (`switch.abc12345_favorite`)
- **Notifications toggle**: Enable/disable notifications (`switch.abc12345_notifications`)
- **Visibility toggles**: Show/hide gate or relay entities (`switch.abc12345_gate_visible`, `switch.abc12345_relay_visible`)

### Sensors

Additional sensors provide detailed monitoring information:

- **Last Action**: Timestamp of the most recent activity (`sensor.abc12345_last_action`)
- **Last User**: Name of the user who performed the last action (`sensor.abc12345_last_user`)
- **Gate Status**: Current gate status with history (`sensor.abc12345_gate_status`)
- **Relay Status**: Current relay status with history (`sensor.abc12345_relay_status`)

### Number Entities

Configuration entities for hardware behavior:

- **Relay Duration**: Control relay pulse duration in milliseconds (`number.abc12345_relay_duration`)
  - Set to `-1` for on/off switch mode (relay stays on until turned off)
  - Set to any positive value (e.g., `1000`) for momentary button mode (relay pulses for that duration)
  - Range: -1 to 30000 milliseconds (30 seconds max)
  - Default: 1000ms (1 second pulse)

### Configuration Entities

Device settings and preferences:

- **Text Entities**: Custom device names for gates and relays
- **Switch Entities**: Enable/disable favorites, notifications, and visibility settings

> **Note**: Replace `abc12345` with your actual device serial number. The integration automatically discovers your devices from the API and creates entities using their serial numbers.

### Device Information

Each entity provides comprehensive attributes:

- **Basic Info**: Device ID, MAC Address, Firmware Version, User Role
- **Status**: Current and latest status from reports
- **Activity**: Last action timestamp and user
- **Behavior**: Gates can stay open; relays are momentary buttons
- **Comparison**: Both device status and report status for accuracy

### Automation Examples

```yaml
# Open gate when arriving home
automation:
  - alias: "Open Gate on Arrival"
    trigger:
      - platform: zone
        entity_id: person.john_doe
        zone: zone.home
        event: enter
    action:
      - service: cover.open_cover
        target:
          entity_id: cover.abc12345_gate

# Send notification when someone opens the gate
automation:
  - alias: "Gate Activity Notification"
    trigger:
      - platform: state
        entity_id: sensor.abc12345_last_user
    action:
      - service: notify.mobile_app_johns_phone
        data:
          message: >
            Gate activity detected!
            {{ states('sensor.abc12345_last_user') }}
            performed an action at
            {{ states('sensor.abc12345_last_action') }}

# Log relay activity with user information
automation:
  - alias: "Log Relay Activity"
    trigger:
      - platform: state
        entity_id: sensor.abc12345_relay_status
    action:
      - service: logbook.log
        data:
          name: "PPA Contatto"
          message: >
            Relay changed to {{ trigger.to_state.state }}
            by {{ states('sensor.abc12345_last_user') }}
          entity_id: sensor.abc12345_relay_status
```

## API Endpoints

The integration uses the following PPA Contatto API endpoints:

- **Authentication**: `https://auth.ppacontatto.com.br/login/password`
- **Device List**: `https://api.ppacontatto.com.br/devices`
- **Device Control**: `https://api.ppacontatto.com.br/device/hardware/{serial}`
- **Device Reports**: `https://api.ppacontatto.com.br/device/{serial}/reports`

## Troubleshooting

### Authentication Issues

- **Invalid Credentials**: Double-check your email and password
- **Network Issues**: Ensure Home Assistant can reach the PPA Contatto servers
- **Account Issues**: Verify your account is active and has device access

### Device Not Appearing

- Ensure the device is set to "show" in your PPA Contatto mobile app
- Check that the device is online and authorized
- Restart the integration if devices were recently added

### Control Issues

- Verify device is online and authorized
- Check Home Assistant logs for specific error messages
- Ensure your account has control permissions for the device

### Logs

Enable debug logging for troubleshooting:

```yaml
logger:
  default: warning
  logs:
    custom_components.ppa_contatto: debug
```

## Development

### Requirements

- Python 3.9+
- Home Assistant 2023.1+
- aiohttp

### Setup Development Environment

1. Clone this repository
2. Install dependencies: `pip install -r requirements.txt`
3. Copy to your Home Assistant `custom_components` directory
4. Restart Home Assistant

### Testing

Test the integration with your PPA Contatto credentials:

```bash
# Test authentication
curl -X POST 'https://auth.ppacontatto.com.br/login/password' \
  -H 'Content-Type: application/json' \
  -d '{"email":"your-email@example.com","password":"your-password"}'

# Test device list (replace YOUR_TOKEN)
curl 'https://api.ppacontatto.com.br/devices' \
  -H 'Authorization: Bearer YOUR_TOKEN'

# Test device reports (replace YOUR_TOKEN and SERIAL)
curl 'https://api.ppacontatto.com.br/device/SERIAL/reports?page=0&total=10' \
  -H 'Authorization: Bearer YOUR_TOKEN'
```

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Add tests if applicable
5. Submit a pull request

## License

This project is licensed under the MIT License - see the LICENSE file for details.

## Contributing

Contributions are welcome! Here's how you can help:

1. **Fork** the repository
2. **Create** a feature branch (`git checkout -b feature/amazing-feature`)
3. **Commit** your changes (`git commit -m 'Add amazing feature'`)
4. **Push** to the branch (`git push origin feature/amazing-feature`)
5. **Open** a Pull Request

### Development Setup

```bash
git clone https://github.com/tarikbc/ha-ppa-contatto.git
cd ha-ppa-contatto
```

### Building Releases

This project includes automated release scripts to ensure proper HACS compatibility:

#### Using the Release Script

```bash
# Run the interactive release builder
./release.sh

# Or run the Python script directly
python3 build_release.py
```

The script will:

1. **Show current version** and suggest next versions (patch/minor/major)
2. **Update manifest.json** with the new version
3. **Create HACS-compatible zip** with correct structure (no extra folder levels)
4. **Git commit and tag** the release
5. **Create GitHub release** with zip file attached

#### Manual Release Process

If you prefer manual releases:

```bash
# 1. Update version in manifest.json
# 2. Create properly structured zip
cd custom_components
zip -r ../ppa_contatto.zip ppa_contatto/ -x "ppa_contatto/__pycache__/*" "ppa_contatto/.DS_Store"

# 3. Create git tag and release
git add .
git commit -m "Release vX.Y.Z"
git tag vX.Y.Z
git push origin main
git push origin vX.Y.Z

# 4. Create GitHub release and upload zip file
```

> **Important**: The zip structure must have integration files at the root, not inside a subfolder. HACS expects the files to be directly accessible when unzipped.

## Support

- **Issues**: Report bugs and request features on [GitHub Issues](https://github.com/tarikbc/ha-ppa-contatto/issues)
- **Discussions**: Ask questions in [GitHub Discussions](https://github.com/tarikbc/ha-ppa-contatto/discussions)

## Disclaimer

This integration is not officially supported by PPA Contatto. Use at your own risk.

**Note to PPA Contatto**: If you're reading this, please consider providing API documentation. Your customers and the developer community would greatly appreciate it! 🙏

---

[commits-shield]: https://img.shields.io/github/commit-activity/y/tarikbc/ha-ppa-contatto.svg?style=for-the-badge
[commits]: https://github.com/tarikbc/ha-ppa-contatto/commits/main
[license-shield]: https://img.shields.io/github/license/tarikbc/ha-ppa-contatto.svg?style=for-the-badge
[maintenance-shield]: https://img.shields.io/badge/maintainer-Tarik%20Caramanico%20%40tarikbc-blue.svg?style=for-the-badge
[releases-shield]: https://img.shields.io/github/release/tarikbc/ha-ppa-contatto.svg?style=for-the-badge
[releases]: https://github.com/tarikbc/ha-ppa-contatto/releases
[user_profile]: https://github.com/tarikbc
