# 360News VPS deployment

The Tencent target provides 4 vCPU, 3.6 GiB RAM, 2 GiB swap and 39 GiB disk. Existing services own ports 80/8000/8080, so the admin uses public port `8787`.

On the VPS:

```sh
git clone https://github.com/smthdagg/RSStT-360News.git /opt/360news
cd /opt/360news/deploy
cp .env.example .env
# fill TOKEN, MANAGER and a PBKDF2 ADMIN_PASSWORD_HASH
docker compose -f docker-compose.vps.yml up -d --build
```

Open `http://YOUR_VPS_IP:8787/` after setting a strong PBKDF2 password. The backend requires Basic Auth and an `X-Admin-Confirm: 1` header for mutations; user management is available to the admin role. Restrict 8787 to your IP in Tencent Security Group/UFW. Do not publish the bridge port. Stop the local Bot before starting the VPS Bot to avoid duplicate Telegram sessions.
