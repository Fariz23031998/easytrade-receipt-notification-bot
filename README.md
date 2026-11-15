# EasyTrade Receipt Notification Bot

Telegram bot that notifies store staff when shifts open/close and (optionally) when sales/returns are posted.

## Requirements

- Python 3.10+
- MySQL (EasyTrade database)
- Telegram bot token in EasyTrade settings (`dir_extensions_settings`, `extss_id=10001`)

## Configuration

Copy the example config and edit secrets locally (do not commit `config.json`):

```bash
cp example.config.json config.json
```

Key settings in `config.json`:

- MySQL connection: `host`, `port`, `user`, `password`, `database`
- `extension_id` — EasyTrade extension settings id for the bot token
- `check_time` — poll interval in seconds
- `lang` — `ru` or `en`
- `activation_code` — shared secret users must send in Telegram before they can subscribe; set a non-default value before production use

## Manual run

```bash
cd /path/to/easytrade-receipt-notification-bot
cp example.config.json config.json   # then edit config.json
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/python main.py
```

## Run as a Linux service (systemd)

A unit file is included: `easytrade-receipt-notification-bot.service`.

1. Create a virtualenv and install dependencies (see above).
2. Adjust `User`, `Group`, `WorkingDirectory`, and `ExecStart` in the unit file to match your install path and OS user.
3. Install, enable, and start the service:

```bash
sudo cp easytrade-receipt-notification-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now easytrade-receipt-notification-bot
```

4. Check status and logs:

```bash
sudo systemctl status easytrade-receipt-notification-bot
sudo journalctl -u easytrade-receipt-notification-bot -f
```

Useful commands:

```bash
sudo systemctl restart easytrade-receipt-notification-bot
sudo systemctl stop easytrade-receipt-notification-bot
sudo systemctl disable easytrade-receipt-notification-bot
```

### User-level service (no root)

If you cannot install a system unit:

```bash
mkdir -p ~/.config/systemd/user
# Copy the unit and remove User=/Group=; set WantedBy=default.target
cp easytrade-receipt-notification-bot.service ~/.config/systemd/user/
# Edit the copied file, then:
systemctl --user daemon-reload
systemctl --user enable --now easytrade-receipt-notification-bot
```

For the user service to start after reboot without an interactive login:

```bash
sudo loginctl enable-linger "$USER"
```
