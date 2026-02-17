# Heat Analyser Login Setup (Auth0 + oauth2-proxy + Nginx)

## 1) In Auth0 anlegen

1. Auth0 Tenant erstellen oder vorhandenen Tenant nutzen.
2. `Applications -> Create Application -> Regular Web Application`.
3. Werte setzen:
   - `Allowed Callback URLs`: `http://YOUR_HOST/oauth2/callback`
   - `Allowed Logout URLs`: `http://YOUR_HOST/`
   - `Allowed Web Origins`: `http://YOUR_HOST`
4. Notieren:
   - `Domain` (z. B. `my-tenant.eu.auth0.com`)
   - `Client ID`
   - `Client Secret`

## 2) oauth2-proxy auf VPS installieren

```bash
cd /tmp
curl -L -o oauth2-proxy.tar.gz https://github.com/oauth2-proxy/oauth2-proxy/releases/download/v7.8.1/oauth2-proxy-v7.8.1.linux-amd64.tar.gz
tar -xzf oauth2-proxy.tar.gz
install -m 0755 oauth2-proxy-v7.8.1.linux-amd64/oauth2-proxy /usr/local/bin/oauth2-proxy
```

## 3) Config-Dateien aus Repo übernehmen

```bash
cd /opt/bmx/bmx-worldcup-analyse
cp deploy/oauth2-proxy.cfg.example /etc/oauth2-proxy.cfg
cp deploy/oauth2-proxy.service.example /etc/systemd/system/oauth2-proxy.service
cp deploy/nginx-heat-analyser-auth0.conf.example /etc/nginx/sites-available/heat-analyser
ln -sf /etc/nginx/sites-available/heat-analyser /etc/nginx/sites-enabled/heat-analyser
```

## 4) Platzhalter ersetzen

`/etc/oauth2-proxy.cfg` anpassen:

- `__AUTH0_DOMAIN__`
- `__AUTH0_CLIENT_ID__`
- `__AUTH0_CLIENT_SECRET__`
- `__PUBLIC_BASE_URL__` (z. B. `http://46.224.186.67`)
- `__COOKIE_SECRET_BASE64_32__`

Cookie Secret erzeugen:

```bash
python3 - <<'PY'
import os, base64
print(base64.urlsafe_b64encode(os.urandom(32)).decode())
PY
```

Falls HTTPS aktiv ist: in `/etc/oauth2-proxy.cfg` `cookie_secure = true`.

## 5) Dienste starten

```bash
systemctl daemon-reload
systemctl enable oauth2-proxy
systemctl restart oauth2-proxy
systemctl status oauth2-proxy --no-pager

nginx -t
systemctl restart nginx
```

## 6) Funktionstest

1. `http://YOUR_HOST` aufrufen.
2. Erwartet: Redirect auf Auth0 Login.
3. Nach Login: Rückkehr zur App.

## 7) Optional: Benutzerzugriff begrenzen

Im Auth0 Tenant:
- nur gewünschte Connections aktivieren
- optional Organization/Role-basierte Freigaben.

In `oauth2-proxy`:
- `email_domains = ["your-domain.com"]` statt `["*"]`.

## 8) Rollback (falls nötig)

```bash
rm -f /etc/nginx/sites-enabled/heat-analyser
systemctl restart nginx
systemctl disable --now oauth2-proxy
```

