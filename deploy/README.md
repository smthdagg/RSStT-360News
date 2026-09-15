# 360News VPS deployment

The Tencent target was checked at `YOUR_VPS_IP`: 4 vCPU, 3.6 GiB RAM, 2 GiB swap and 39 GiB disk. Existing services own ports 80/8000/8080, so the admin is intentionally loopback-only on `127.0.0.1:8787`.

On the VPS:

```sh
git clone https://github.com/smthdagg/RSStT-360News.git /opt/360news
cd /opt/360news/deploy
cp .env.example .env
# fill TOKEN, MANAGER and a PBKDF2 ADMIN_PASSWORD_HASH
docker compose -f docker-compose.vps.yml up -d --build
ssh -L 8787:127.0.0.1:8787 ubuntu@YOUR_VPS_IP
```

Open `http://127.0.0.1:8787/`. Do not publish the Docker socket or bridge port. Stop the local Bot before starting the VPS Bot to avoid duplicate Telegram sessions.
