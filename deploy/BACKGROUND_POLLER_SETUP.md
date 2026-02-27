# Background Poller Setup (systemd)

## 1) Install service template once

```bash
cp /opt/bmx/bmx-worldcup-analyse/deploy/bmx-poller@.service.example /etc/systemd/system/bmx-poller@.service
mkdir -p /etc/bmx-pollers
chmod 700 /etc/bmx-pollers
systemctl daemon-reload
```

## 2) App-managed pollers

After setup, use the Streamlit sidebar section **Live Polling (Service)** to:

- create/update poller config (`/etc/bmx-pollers/<instance>.env`)
- start poller (`systemctl start bmx-poller@<instance>`)
- stop poller
- inspect status + logs

## 3) Manual check

```bash
systemctl list-units 'bmx-poller@*.service'
journalctl -u bmx-poller@<instance> -n 80 --no-pager
```
